import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { emptyDsl, type EditorDsl } from "@/lib/dsl/document"

import { createSaveStore, SAVE_DELAY_MS, type SaveRequest, type SaveResult, type SaveStore } from "./save"

/** A save function whose answers the test hands out one at a time. */
function stubSave() {
  const calls: { draftDsl: EditorDsl; revision: number }[] = []
  let answers: SaveResult[] = []
  let pending: ((result: SaveResult) => void) | null = null

  const save: SaveRequest = (body) => {
    calls.push(body)
    const next = answers.shift()
    if (next !== undefined) return Promise.resolve(next)
    // No answer queued: the request hangs until the test releases it, which is how "in flight" is
    // expressed without a real clock.
    return new Promise<SaveResult>((resolve) => {
      pending = resolve
    })
  }

  return {
    save,
    calls,
    queue(...results: SaveResult[]) {
      answers = [...answers, ...results]
    },
    release(result: SaveResult) {
      const resolve = pending
      pending = null
      resolve?.(result)
    },
  }
}

let stub: ReturnType<typeof stubSave>
let reloaded: EditorDsl[]
let dsl: EditorDsl
let store: SaveStore

function make(revision = 1) {
  return createSaveStore({
    revision,
    getDsl: () => dsl,
    save: stub.save,
    onReload: (next) => {
      reloaded.push(next)
      dsl = next
    },
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  stub = stubSave()
  reloaded = []
  dsl = emptyDsl()
  store = make()
})

afterEach(() => {
  vi.useRealTimers()
})

/** Let the store's promise chain settle; the timers are fake, the microtasks are not. */
async function settle() {
  await vi.advanceTimersByTimeAsync(0)
}

describe("debouncing", () => {
  it("saves one second after a change", async () => {
    stub.queue({ outcome: "saved", revision: 2 })
    store.getState().changed()

    expect(stub.calls).toHaveLength(0)
    expect(store.getState().status).toBe("pending")

    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)
    expect(stub.calls).toHaveLength(1)
  })

  it("collapses three changes in half a second into one save", async () => {
    stub.queue({ outcome: "saved", revision: 2 })
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(200)
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(200)
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)

    expect(stub.calls).toHaveLength(1)
  })

  it("sends the document as it is at save time, not as it was when the timer started", async () => {
    stub.queue({ outcome: "saved", revision: 2 })
    store.getState().changed()
    dsl = { ...emptyDsl(), nodes: [{ id: "start", type: "start", position: { x: 1, y: 1 } }] }
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)

    expect(stub.calls[0]?.draftDsl.nodes).toHaveLength(1)
  })

  it("adopts the revision the engine returns", async () => {
    stub.queue({ outcome: "saved", revision: 7 })
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)

    expect(store.getState().revision).toBe(7)
    expect(store.getState().status).toBe("saved")
    expect(store.getState().savedAt).not.toBeNull()
  })

  it("saves immediately on flush", async () => {
    stub.queue({ outcome: "saved", revision: 2 })
    store.getState().changed()
    await store.getState().flush()

    expect(stub.calls).toHaveLength(1)
  })
})

describe("a change while a save is in the air", () => {
  it("produces exactly one follow-up save, whatever the change count", async () => {
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)
    expect(stub.calls).toHaveLength(1)

    store.getState().changed()
    store.getState().changed()
    store.getState().changed()
    expect(stub.calls).toHaveLength(1)

    stub.queue({ outcome: "saved", revision: 3 })
    stub.release({ outcome: "saved", revision: 2 })
    await settle()

    expect(stub.calls).toHaveLength(2)
    // On the revision the first save produced, not the one it was sent with.
    expect(stub.calls[1]?.revision).toBe(2)
  })

  it("does not rest at 저장됨 while a follow-up is still in the air", async () => {
    // The status bar is the only thing telling someone whether it is safe to close the tab, so it must
    // not read 저장됨 with an unsaved change still going out.
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)
    store.getState().changed()

    stub.release({ outcome: "saved", revision: 2 })
    await settle()

    // The follow-up started, so the bar reads 저장 중 -- not 저장됨.
    expect(store.getState().status).toBe("saving")
    expect(stub.calls).toHaveLength(2)

    stub.release({ outcome: "saved", revision: 3 })
    await settle()
    expect(store.getState().status).toBe("saved")
    expect(store.getState().revision).toBe(3)
  })

  it("does not follow up when nothing changed during the save", async () => {
    stub.queue({ outcome: "saved", revision: 2 })
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)
    await settle()

    expect(stub.calls).toHaveLength(1)
    expect(store.getState().status).toBe("saved")
  })
})

describe("a conflict", () => {
  const THEIRS: EditorDsl = {
    ...emptyDsl(),
    nodes: [{ id: "start", type: "start", position: { x: 9, y: 9 } }],
  }

  async function conflict() {
    stub.queue({ outcome: "conflict", currentRevision: 5, draftDsl: THEIRS })
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)
    await settle()
  }

  it("opens the dialog and does not retry on its own", async () => {
    await conflict()

    expect(store.getState().status).toBe("conflict")
    expect(store.getState().conflict).toEqual({ currentRevision: 5, draftDsl: THEIRS })

    // Nothing else goes out, however long nobody answers.
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS * 30)
    expect(stub.calls).toHaveLength(1)
  })

  it("keeps saving stopped while it is unresolved", async () => {
    await conflict()
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS * 5)

    // Saving over the other side's work is exactly what the dialog exists to prevent.
    expect(stub.calls).toHaveLength(1)
    expect(store.getState().status).toBe("conflict")
  })

  it("never replaces the document unless the person chose to", async () => {
    await conflict()

    expect(reloaded).toEqual([])
    expect(dsl.nodes).toHaveLength(0)
  })

  it("불러오기 takes their draft and their revision", async () => {
    await conflict()
    store.getState().reload()

    expect(reloaded).toEqual([THEIRS])
    expect(store.getState().revision).toBe(5)
    expect(store.getState().status).toBe("saved")
    expect(store.getState().conflict).toBeNull()
  })

  it("덮어쓰기 resends this document against the revision that won", async () => {
    await conflict()
    stub.queue({ outcome: "saved", revision: 6 })
    await store.getState().overwrite()

    expect(stub.calls[1]?.revision).toBe(5)
    expect(stub.calls[1]?.draftDsl).toEqual(dsl)
    expect(store.getState().revision).toBe(6)
    expect(store.getState().status).toBe("saved")
  })

  it("asks again on a second 409 rather than looping", async () => {
    // Two tabs retrying at each other is a race where whoever typed last wins and the other's work is
    // gone. The person's click is the one retry.
    await conflict()
    stub.queue({ outcome: "conflict", currentRevision: 8, draftDsl: THEIRS })
    await store.getState().overwrite()

    expect(stub.calls).toHaveLength(2)
    expect(store.getState().status).toBe("conflict")
    expect(store.getState().conflict?.currentRevision).toBe(8)

    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS * 30)
    expect(stub.calls).toHaveLength(2)
  })

  it("does nothing when asked to resolve a conflict that is not there", async () => {
    store.getState().reload()
    await store.getState().overwrite()

    expect(reloaded).toEqual([])
    expect(stub.calls).toHaveLength(0)
  })
})

describe("a failure that is not a conflict", () => {
  it("leaves the document alone and says so", async () => {
    stub.queue({ outcome: "failed", message: "엔진에 연결하지 못했습니다" })
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)
    await settle()

    expect(store.getState().status).toBe("error")
    expect(store.getState().error).toBe("엔진에 연결하지 못했습니다")
    expect(reloaded).toEqual([])
    expect(store.getState().revision).toBe(1)
  })

  it("does not retry on its own", async () => {
    stub.queue({ outcome: "failed", message: "실패" })
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS * 30)

    // A failing engine does not need this editor knocking every second, and the work is safe in the
    // browser meanwhile.
    expect(stub.calls).toHaveLength(1)
  })

  it("retries on the next change", async () => {
    stub.queue({ outcome: "failed", message: "실패" }, { outcome: "saved", revision: 2 })
    store.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)
    await settle()

    store.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)
    await settle()

    expect(stub.calls).toHaveLength(2)
    expect(store.getState().status).toBe("saved")
  })

  it("treats a thrown request as a failure, not a crash", async () => {
    const throwing = createSaveStore({
      revision: 1,
      getDsl: () => dsl,
      save: () => Promise.reject(new Error("네트워크 오류")),
      onReload: () => {},
    })
    throwing.getState().changed()
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS)
    await settle()

    expect(throwing.getState().status).toBe("error")
    expect(throwing.getState().error).toBe("네트워크 오류")
  })
})
