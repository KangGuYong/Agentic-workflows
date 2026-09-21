import { act, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { RunEvent } from "@/lib/run/events"

import { useRunStream } from "./useRunStream"

/** A stand-in for the browser's `EventSource`, which jsdom does not have.
 *
 * It records every instance so a test can assert how many were opened -- the property that matters
 * most here and the one a real `EventSource` would hide.
 */
class FakeEventSource {
  static opened: FakeEventSource[] = []

  readonly listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>()
  onerror: (() => void) | null = null
  closed = false

  constructor(readonly url: string) {
    FakeEventSource.opened.push(this)
  }

  addEventListener(type: string, handler: (event: MessageEvent<string>) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), handler])
  }

  close() {
    this.closed = true
  }

  /** Deliver a frame the way the server would: named by `event:`, carrying JSON in `data:`. */
  send(event: RunEvent) {
    for (const handler of this.listeners.get(event.type) ?? []) {
      handler(new MessageEvent(event.type, { data: JSON.stringify(event) }))
    }
  }

  sendRaw(type: string, data: string) {
    for (const handler of this.listeners.get(type) ?? []) {
      handler(new MessageEvent(type, { data }))
    }
  }

  static get live() {
    return FakeEventSource.opened.filter((source) => !source.closed)
  }

  static get last() {
    return FakeEventSource.opened[FakeEventSource.opened.length - 1]
  }
}

function Probe({ runId }: { runId: string | null }) {
  const stream = useRunStream(runId)
  return (
    <div>
      <span data-testid="status">{stream.status}</span>
      <span data-testid="finished">{String(stream.finished)}</span>
      <span data-testid="disconnected">{String(stream.disconnected)}</span>
      <span data-testid="llm">{stream.nodes["llm_1"]?.status ?? "-"}</span>
      <span data-testid="tokens">{stream.nodes["llm_1"]?.tokens ?? ""}</span>
    </div>
  )
}

function text(id: string) {
  return screen.getByTestId(id).textContent
}

beforeEach(() => {
  FakeEventSource.opened = []
  vi.stubGlobal("EventSource", FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("opening the stream", () => {
  it("opens nothing without a run", () => {
    render(<Probe runId={null} />)

    expect(FakeEventSource.opened).toHaveLength(0)
  })

  it("opens the run's stream through the proxy", () => {
    render(<Probe runId="r1" />)

    expect(FakeEventSource.last?.url).toBe("/api/engine/runs/r1/events")
  })

  it("escapes the run id", () => {
    render(<Probe runId="../secrets" />)

    expect(FakeEventSource.last?.url).toBe("/api/engine/runs/..%2Fsecrets/events")
  })

  it("opens exactly one, however many times the component re-renders", () => {
    // Two live connections double every event, and the reducer counts each twice.
    const { rerender } = render(<Probe runId="r1" />)
    rerender(<Probe runId="r1" />)
    rerender(<Probe runId="r1" />)

    expect(FakeEventSource.opened).toHaveLength(1)
  })

  it("closes the old one and opens a new one for a different run", () => {
    const { rerender } = render(<Probe runId="r1" />)
    const first = FakeEventSource.last
    rerender(<Probe runId="r2" />)

    expect(first?.closed).toBe(true)
    expect(FakeEventSource.live).toHaveLength(1)
  })

  it("closes it when the component goes away", () => {
    const { unmount } = render(<Probe runId="r1" />)
    unmount()

    expect(FakeEventSource.last?.closed).toBe(true)
  })

  it("starts from nothing when the run changes", () => {
    const { rerender } = render(<Probe runId="r1" />)
    act(() => FakeEventSource.last?.send({ type: "run_started", seq: 1 }))
    expect(text("status")).toBe("running")

    rerender(<Probe runId="r2" />)
    expect(text("status")).toBe("queued")
  })
})

describe("frames", () => {
  it("drives the run and node status", () => {
    render(<Probe runId="r1" />)
    act(() => {
      FakeEventSource.last?.send({ type: "run_started", seq: 1 })
      FakeEventSource.last?.send({ type: "node_started", seq: 2, nodeId: "llm_1", attempt: 1 })
    })

    expect(text("status")).toBe("running")
    expect(text("llm")).toBe("running")
  })

  it("appends tokens", () => {
    render(<Probe runId="r1" />)
    act(() => {
      FakeEventSource.last?.send({ type: "node_started", seq: 1, nodeId: "llm_1" })
      FakeEventSource.last?.send({ type: "node_token", nodeId: "llm_1", text: "안녕" })
      FakeEventSource.last?.send({ type: "node_token", nodeId: "llm_1", text: "하세요" })
    })

    expect(text("tokens")).toBe("안녕하세요")
  })

  it("ignores a frame that is not JSON rather than tearing down the stream", () => {
    render(<Probe runId="r1" />)
    act(() => {
      FakeEventSource.last?.sendRaw("node_token", "not json")
      FakeEventSource.last?.send({ type: "run_started", seq: 1 })
    })

    expect(text("status")).toBe("running")
    expect(FakeEventSource.last?.closed).toBe(false)
  })
})

describe("the end of a run", () => {
  it("closes the connection on a terminal event", () => {
    // `EventSource` reconnects whenever the server closes a stream. Left open, a finished run would be
    // reopened forever, replaying itself each time.
    render(<Probe runId="r1" />)
    act(() => FakeEventSource.last?.send({ type: "run_succeeded", seq: 9 }))

    expect(FakeEventSource.last?.closed).toBe(true)
    expect(text("finished")).toBe("true")
  })

  it("opens no further connection after that", () => {
    const { rerender } = render(<Probe runId="r1" />)
    act(() => FakeEventSource.last?.send({ type: "run_succeeded", seq: 9 }))
    rerender(<Probe runId="r1" />)
    rerender(<Probe runId="r1" />)

    expect(FakeEventSource.opened).toHaveLength(1)
  })

  it("closes on a failure and a cancellation too", () => {
    for (const type of ["run_failed", "run_cancelled"]) {
      FakeEventSource.opened = []
      const { unmount } = render(<Probe runId="r1" />)
      act(() => FakeEventSource.last?.send({ type, seq: 9 }))
      expect(FakeEventSource.last?.closed, type).toBe(true)
      unmount()
    }
  })
})

describe("a dropped connection", () => {
  it("says so while the run is still going, without closing anything", () => {
    // `EventSource` is already backing off and resending `Last-Event-ID`. A reconnection of our own on
    // top of that would fight it.
    render(<Probe runId="r1" />)
    act(() => {
      FakeEventSource.last?.send({ type: "run_started", seq: 1 })
      FakeEventSource.last?.onerror?.()
    })

    expect(text("disconnected")).toBe("true")
    expect(FakeEventSource.last?.closed).toBe(false)
  })

  it("clears it on the next message", () => {
    render(<Probe runId="r1" />)
    act(() => {
      FakeEventSource.last?.send({ type: "run_started", seq: 1 })
      FakeEventSource.last?.onerror?.()
    })
    act(() => FakeEventSource.last?.send({ type: "node_started", seq: 2, nodeId: "llm_1" }))

    expect(text("disconnected")).toBe("false")
  })

  it("closes for good when the error comes after the run ended", () => {
    // The server closing the stream after a terminal event is the stream working, not a fault.
    render(<Probe runId="r1" />)
    act(() => FakeEventSource.last?.send({ type: "run_succeeded", seq: 9 }))
    act(() => FakeEventSource.last?.onerror?.())

    expect(text("disconnected")).toBe("false")
    expect(FakeEventSource.last?.closed).toBe(true)
  })
})
