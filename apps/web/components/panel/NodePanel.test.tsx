import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it } from "vitest"
import { useStore } from "zustand"

import { emptyDsl, type EditorNode } from "@/lib/dsl/document"
import type { NodeType } from "@/lib/palette"
import { createGraphStore, type GraphStore } from "@/store/graph"

import { NodePanel } from "./NodePanel"

const TEMPLATE_TYPE: NodeType = {
  type: "template",
  label: "템플릿",
  category: "Logic",
  isBranch: false,
  sideEffects: false,
  configSchema: {
    type: "object",
    properties: {
      template: { type: "string", "x-template": true },
      format: { type: "string", enum: ["text", "json"] },
    },
  },
  // The engine reports null for a node type with nothing to override.
  defaultPolicy: null,
}

const LLM_TYPE: NodeType = {
  type: "llm",
  label: "LLM",
  category: "AI",
  isBranch: false,
  sideEffects: false,
  configSchema: {
    // As the engine sends it: pydantic closes every model, and an optional schema field arrives as an
    // `anyOf` with a null branch.
    type: "object",
    additionalProperties: false,
    properties: {
      model: { type: "string" },
      prompt: { type: "string", "x-template": true },
      outputSchema: { anyOf: [{ type: "object", additionalProperties: true }, { type: "null" }], default: null },
    },
  },
  defaultPolicy: {
    timeoutSec: 120,
    retry: { maxAttempts: 3, backoff: "exponential", initialDelaySec: 2 },
    onError: "fail",
    defaultOutput: null,
  },
}

const HTTP_TYPE: NodeType = {
  type: "http_request",
  label: "HTTP 요청",
  category: "Action",
  isBranch: false,
  sideEffects: true,
  configSchema: {
    type: "object",
    properties: {
      method: { type: "string", enum: ["GET", "POST"] },
      url: { type: "string", "x-template": true },
      // `dict[str, str]` in the engine.
      headers: { type: "object", additionalProperties: { type: "string" } },
    },
  },
  defaultPolicy: {
    timeoutSec: 30,
    retry: { maxAttempts: 3, backoff: "exponential", initialDelaySec: 2 },
    onError: "fail",
    defaultOutput: null,
  },
}

let store: GraphStore

beforeEach(() => {
  store = createGraphStore(emptyDsl())
})

function node(id: string, type: string, extra: Partial<EditorNode> = {}): EditorNode {
  return { id, type, position: { x: 0, y: 0 }, ...extra }
}

function show(editorNode: EditorNode, nodeType: NodeType | undefined) {
  return render(<NodePanel node={editorNode} nodeType={nodeType} state={store.getState()} />)
}

/** The panel as `Canvas` mounts it: reading the node back out of the store on every change.
 *
 * `show` passes a frozen node, which is enough for what a single tab renders. It is not enough for an
 * edit in one tab that changes another -- the method/attempts coupling below only exists across a
 * re-render driven by the store. */
function Live({ nodeId, nodeType }: { nodeId: string; nodeType: NodeType }) {
  const state = useStore(store)
  const editorNode = state.dsl.nodes.find((candidate) => candidate.id === nodeId)
  return editorNode === undefined ? null : (
    <NodePanel node={editorNode} nodeType={nodeType} state={state} />
  )
}

describe("which tabs appear", () => {
  it("omits 실행 정책 for a node type the engine gives no default policy", () => {
    show(node("template_1", "template"), TEMPLATE_TYPE)

    expect(screen.getByRole("tab", { name: "설정" })).toBeInTheDocument()
    expect(screen.getByRole("tab", { name: "라벨" })).toBeInTheDocument()
    expect(screen.queryByRole("tab", { name: "실행 정책" })).toBeNull()
  })

  it("shows all three for a node type that has one", () => {
    show(node("llm_1", "llm"), LLM_TYPE)

    expect(screen.getByRole("tab", { name: "실행 정책" })).toBeInTheDocument()
  })

  it("names the node by its label, falling back to the type's", () => {
    const { unmount } = show(node("llm_1", "llm"), LLM_TYPE)
    expect(screen.getByText("LLM")).toBeInTheDocument()
    unmount()

    show(node("llm_1", "llm", { label: "요약" }), LLM_TYPE)
    expect(screen.getByText("요약")).toBeInTheDocument()
  })

  it("says so rather than rendering nothing for an unknown node type", () => {
    // A workflow saved against a newer engine can hold a type this editor does not know.
    show(node("mystery_1", "mystery"), undefined)

    expect(screen.getByText(/설정 형식을 알 수 없습니다/)).toBeInTheDocument()
  })
})

describe("the settings tab", () => {
  it("lets a free-form map gain a key", async () => {
    // `headers` is the engine's `dict[str, str]`. Without an add button the field renders as a heading
    // over nothing, and `http_request` cannot set a header at all -- which is most of what it is for.
    show(node("http_request_1", "http_request", { config: { method: "GET" } }), HTTP_TYPE)

    const add = screen.getByRole("button", { name: /항목 추가/ })
    await userEvent.click(add)

    // RJSF names the new key field after the property path; its presence is what proves the click did
    // something the form can carry.
    expect(screen.getAllByRole("textbox").length).toBeGreaterThan(1)
  })

  it("does not show the pydantic class name as a heading", () => {
    // `configSchema.title` is `HttpRequestConfig`, which means nothing to a user and repeats the panel
    // header two lines above it.
    show(node("http_request_1", "http_request"), {
      ...HTTP_TYPE,
      configSchema: { ...HTTP_TYPE.configSchema, title: "HttpRequestConfig" },
    })

    expect(screen.queryByText("HttpRequestConfig")).toBeNull()
  })
})

describe("the policy tab", () => {
  it("shows the engine's defaults for a node that overrides nothing", async () => {
    show(node("llm_1", "llm"), LLM_TYPE)
    await userEvent.click(screen.getByRole("tab", { name: "실행 정책" }))

    expect(screen.getByLabelText(/제한 시간/)).toHaveValue(120)
    expect(screen.getByLabelText(/시도 횟수/)).toHaveValue(3)
  })

  it("shows the node's override laid over those defaults", async () => {
    show(node("llm_1", "llm", { policy: { timeoutSec: 30 } }), LLM_TYPE)
    await userEvent.click(screen.getByRole("tab", { name: "실행 정책" }))

    expect(screen.getByLabelText(/제한 시간/)).toHaveValue(30)
    // Not blank: the node overrides only the timeout, so attempts still follows the engine.
    expect(screen.getByLabelText(/시도 횟수/)).toHaveValue(3)
  })

  it("locks the attempt count for an http_request the engine will not retry", async () => {
    show(node("http_request_1", "http_request", { config: { method: "POST" } }), HTTP_TYPE)
    await userEvent.click(screen.getByRole("tab", { name: "실행 정책" }))

    const attempts = screen.getByLabelText(/시도 횟수/)
    expect(attempts).toBeDisabled()
    expect(attempts).toHaveValue(1)
    expect(screen.getByText(/POST·PATCH는 재시도하지 않습니다/)).toBeInTheDocument()
  })

  it("locks it the moment the method is switched GET -> POST", async () => {
    // The two tests above each render one fixed method. This is the transition a person actually makes:
    // the attempts field lives in a different tab from the method, so nothing re-renders it unless the
    // panel reads the node back out of the store.
    store.getState().addNodeAt("http_request", { x: 0, y: 0 })
    const placed = store.getState().dsl.nodes[0] as EditorNode
    store.getState().setNodeConfig(placed.id, { method: "GET" })
    render(<Live nodeId={placed.id} nodeType={HTTP_TYPE} />)

    await userEvent.click(screen.getByRole("tab", { name: "실행 정책" }))
    expect(screen.getByLabelText(/시도 횟수/)).toBeEnabled()

    await userEvent.click(screen.getByRole("tab", { name: "설정" }))
    await userEvent.selectOptions(screen.getByLabelText("메서드"), "POST")

    await userEvent.click(screen.getByRole("tab", { name: "실행 정책" }))
    expect(screen.getByLabelText(/시도 횟수/)).toBeDisabled()
    expect(screen.getByText(/POST·PATCH는 재시도하지 않습니다/)).toBeInTheDocument()
  })

  it("leaves it editable for a method the engine does retry", async () => {
    show(node("http_request_1", "http_request", { config: { method: "GET" } }), HTTP_TYPE)
    await userEvent.click(screen.getByRole("tab", { name: "실행 정책" }))

    expect(screen.getByLabelText(/시도 횟수/)).toBeEnabled()
    expect(screen.queryByText(/재시도하지 않습니다/)).toBeNull()
  })

  it("offers the reset only when there is something to reset", async () => {
    const { unmount } = show(node("llm_1", "llm"), LLM_TYPE)
    await userEvent.click(screen.getByRole("tab", { name: "실행 정책" }))
    expect(screen.getByRole("button", { name: /기본값으로 되돌리기/ })).toBeDisabled()
    unmount()

    show(node("llm_1", "llm", { policy: { timeoutSec: 30 } }), LLM_TYPE)
    await userEvent.click(screen.getByRole("tab", { name: "실행 정책" }))
    expect(screen.getByRole("button", { name: /기본값으로 되돌리기/ })).toBeEnabled()
  })
})

describe("the label tab", () => {
  it("writes the label through the store", async () => {
    store.getState().addNodeAt("llm", { x: 0, y: 0 })
    const placed = store.getState().dsl.nodes[0] as EditorNode
    show(placed, LLM_TYPE)

    await userEvent.click(screen.getByRole("tab", { name: "라벨" }))
    await userEvent.type(screen.getByLabelText("라벨"), "요")

    expect(store.getState().dsl.nodes[0]?.label).toBe("요")
  })
})

describe("the template field", () => {
  it("renders the template editor, not a plain textarea", async () => {
    show(node("llm_1", "llm"), LLM_TYPE)

    // `prompt` carries `x-template`, so `buildUiSchema` routes it to the template widget.
    expect(await screen.findByTestId("template-editor")).toBeInTheDocument()
  })

  it("puts the engine's template rules under it, collapsed", async () => {
    show(node("llm_1", "llm"), LLM_TYPE)
    await screen.findByTestId("template-editor")

    const help = screen.getByText("템플릿 작성 규칙")
    expect(help).toBeInTheDocument()
    // Collapsed: seven rules pushing the canvas off the screen is not what someone editing their tenth
    // template needs.
    expect(help.closest("details")?.open).toBe(false)
  })

  it("does not render it for a field that is not a template", () => {
    // `model` is a plain string. A CodeMirror instance per string field would be absurd.
    show(node("llm_1", "llm"), {
      ...LLM_TYPE,
      configSchema: { type: "object", properties: { model: { type: "string" } } },
    })

    expect(screen.queryByTestId("template-editor")).toBeNull()
  })
})

describe("the shape of the generated form", () => {
  it("offers no add button on a closed schema", async () => {
    // Pydantic emits `additionalProperties: false` on every model. Reading that as "a free-form map"
    // put a dead + 항목 추가 button under every settings form.
    show(node("llm_1", "llm"), LLM_TYPE)
    await screen.findByTestId("template-editor")

    // Exactly this label: the schema editor's own add button says 필드 추가, because "add a key to this
    // map" and "add a field to this data format" are different things and were sharing one word.
    expect(screen.queryByRole("button", { name: "+ 항목 추가" })).toBeNull()
  })

  it("still offers one on the map that is genuinely open", () => {
    // The control: `http_request.headers` is `dict[str, str]`.
    show(node("http_request_1", "http_request", { config: { method: "GET" } }), HTTP_TYPE)

    expect(screen.getByRole("button", { name: /항목 추가/ })).toBeInTheDocument()
  })

  it("renders the schema editor for an optional schema field, not RJSF's anyOf selector", async () => {
    // `outputSchema` is `anyOf: [{object}, {null}]`. RJSF renders an anyOf with its own option selector
    // before it consults `ui:widget`, so the panel showed a number input holding the branch index.
    show(node("llm_1", "llm"), LLM_TYPE)
    await screen.findByTestId("template-editor")

    // A group of controls, named by its legend -- the schema editor is a form, not one input.
    const schemaField = screen.getByRole("group", { name: "출력 형식" })
    expect(schemaField).toBeInTheDocument()
    // And *only* that: `ui:field` replaces how the value is edited but leaves RJSF's branch selector
    // rendered beside it, which is why the schema is collapsed before the form ever sees it.
    expect(screen.queryByRole("combobox")).toBeNull()
  })

  it("shows the template rules once, not once per template field", async () => {
    // `llm` has two template fields in the engine (system and prompt). Two copies of a seven-rule
    // explainer in a 320px panel is clutter.
    show(node("llm_1", "llm"), {
      ...LLM_TYPE,
      configSchema: {
        type: "object",
        properties: {
          system: { type: "string", "x-template": true },
          prompt: { type: "string", "x-template": true },
        },
      },
    })
    await screen.findAllByTestId("template-editor")

    expect(screen.getAllByText("템플릿 작성 규칙")).toHaveLength(1)
  })

  it("shows no template rules for a node type that has no template field", () => {
    show(node("merge_1", "merge"), {
      ...LLM_TYPE,
      type: "merge",
      configSchema: { type: "object", properties: { mode: { type: "string" } } },
    })

    expect(screen.queryByText("템플릿 작성 규칙")).toBeNull()
  })
})
