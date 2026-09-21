import { createStore, type StoreApi } from "zustand/vanilla"

import {
  addNode,
  connect,
  disconnect,
  removeNode,
  setConfig,
  setLabel,
  setPolicy,
  setPositions,
  type Connection,
} from "@/lib/dsl/commands"
import type { EditorDsl, NodeConfig, Policy, XY } from "@/lib/dsl/document"
import { movedPositions, type NodeChange } from "@/lib/dsl/flow"
import { layout } from "@/lib/dsl/layout"
import {
  apply,
  canRedo as historyCanRedo,
  canUndo as historyCanUndo,
  commit,
  createHistory,
  redo as historyRedo,
  undo as historyUndo,
  type History,
} from "@/lib/dsl/history"

/** The canvas's state: the document, its history, and what is selected (3 설계 §3.1).
 *
 * The document lives here and React Flow renders a view of it, rather than React Flow holding the state
 * and the document being derived from it. That is what makes undo, autosave and validation read from
 * one place instead of three.
 *
 * `createGraphStore` returns a fresh store so tests get isolation; the app binds one instance per
 * editor screen.
 */

export interface Selection {
  nodes: string[]
  edges: string[]
}

export interface GraphState {
  dsl: EditorDsl
  history: History
  selection: Selection
  canUndo: boolean
  canRedo: boolean
  /** Why the last edit was refused, for the UI to show. Cleared by the next edit that succeeds. */
  lastError: string | null

  addNodeAt: (type: string, position: XY) => void
  connectNodes: (connection: Connection) => void
  removeSelected: () => void
  moveNodes: (changes: NodeChange[]) => void
  endDrag: () => void
  setNodeConfig: (nodeId: string, config: NodeConfig) => void
  setNodeLabel: (nodeId: string, label: string) => void
  setNodePolicy: (nodeId: string, policy: Policy | undefined) => void
  /** Ends the merge window on a panel field, so the next edit starts a new undo step. */
  endEdit: () => void
  autoLayout: () => Promise<void>
  /** True while ELK is running, so the button can disable itself rather than stack layouts. */
  layingOut: boolean
  select: (selection: Selection) => void
  undo: () => void
  redo: () => void
  /** Replace the whole document, discarding the history.
   *
   * Only for taking someone else's draft on a save conflict (3 설계 §9). The history goes with it on
   * purpose: an undo that reached back past the replacement would resurrect the local draft the person
   * just chose to abandon, and autosave would then write it over theirs -- the overwrite they declined.
   */
  replaceDocument: (dsl: EditorDsl) => void
}

export type GraphStore = StoreApi<GraphState>

const NOTHING: Selection = { nodes: [], edges: [] }

function derived(history: History) {
  return {
    dsl: history.present,
    history,
    canUndo: historyCanUndo(history),
    canRedo: historyCanRedo(history),
  }
}

export function createGraphStore(initial: EditorDsl): GraphStore {
  return createStore<GraphState>((set, get) => {
    /** Run a command and record it, unless it changed nothing. */
    function edit(next: EditorDsl, mergeKey?: string) {
      set({ ...derived(apply(get().history, next, { mergeKey })), lastError: null })
    }

    return {
      ...derived(createHistory(initial)),
      selection: NOTHING,
      lastError: null,
      layingOut: false,

      addNodeAt(type, position) {
        let next: EditorDsl
        try {
          next = addNode(get().dsl, type, position)
        } catch (error) {
          // A drop handler cannot let an exception escape into a drag event, and the user still needs
          // to know why nothing appeared.
          set({ lastError: error instanceof Error ? error.message : String(error) })
          return
        }
        const added = next.nodes.at(-1)
        edit(next)
        // Selecting it opens the settings panel on what was just placed, which is what the user is
        // about to configure.
        if (added !== undefined) set({ selection: { nodes: [added.id], edges: [] } })
      },

      connectNodes(connection) {
        edit(connect(get().dsl, connection))
      },

      removeSelected() {
        const { dsl, selection } = get()
        if (selection.nodes.length === 0 && selection.edges.length === 0) return
        // One command for the whole selection, so one undo brings it all back rather than as many
        // presses as there were items.
        let next = dsl
        for (const edgeId of selection.edges) next = disconnect(next, edgeId)
        for (const nodeId of selection.nodes) next = removeNode(next, nodeId)
        edit(next)
        set({ selection: NOTHING })
      },

      moveNodes(changes) {
        const moved = movedPositions(changes)
        const ids = Object.keys(moved)
        if (ids.length === 0) return
        // Keyed on the whole set being dragged: a multi-select drag is one step, and picking up a
        // different node starts a new one.
        edit(setPositions(get().dsl, moved), `position:${[...ids].sort().join(",")}`)
      },

      endDrag() {
        set({ history: commit(get().history) })
      },

      // The three panel edits share one shape: merge consecutive changes to the *same field of the same
      // node* into one undo step, so typing a prompt is one step rather than one per keystroke. The plan
      // called for a 300ms debounce; merging does the same job without dropping the last keystroke of a
      // burst and without a timer for tests to race (see the Task 9 note).
      setNodeConfig(nodeId, config) {
        edit(setConfig(get().dsl, nodeId, config), `config:${nodeId}`)
      },

      setNodeLabel(nodeId, label) {
        edit(setLabel(get().dsl, nodeId, label), `label:${nodeId}`)
      },

      setNodePolicy(nodeId, policy) {
        edit(setPolicy(get().dsl, nodeId, policy ?? {}), `policy:${nodeId}`)
      },

      endEdit() {
        set({ history: commit(get().history) })
      },

      async autoLayout() {
        if (get().layingOut) return
        set({ layingOut: true })
        try {
          const moved = await layout(get().dsl)
          // One command for every node, so one undo puts the whole graph back where it was. Laying out
          // node by node would make undo as many presses as there are nodes.
          edit(setPositions(get().dsl, moved))
        } catch (error) {
          set({ lastError: error instanceof Error ? error.message : String(error) })
        } finally {
          set({ layingOut: false })
        }
      },

      replaceDocument(dsl) {
        set({ ...derived(createHistory(dsl)), selection: NOTHING, lastError: null })
      },

      select(selection) {
        set({ selection })
      },

      undo() {
        // The selection can point at a node the undone state does not have.
        set({ ...derived(historyUndo(get().history)), selection: NOTHING })
      },

      redo() {
        set({ ...derived(historyRedo(get().history)), selection: NOTHING })
      },
    }
  })
}
