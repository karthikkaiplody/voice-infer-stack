import { createContext, useContext, useReducer, type Dispatch, type ReactNode } from "react";
import { useEventStream } from "../stream/useEventStream";
import { initialState, reducer, type Action, type ViewState } from "./reducer";

interface Store {
  state: ViewState;
  dispatch: Dispatch<Action>;
}

const StoreContext = createContext<Store | null>(null);

export function StoreProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  useEventStream(dispatch);
  return <StoreContext.Provider value={{ state, dispatch }}>{children}</StoreContext.Provider>;
}

/** Test seam: provide a fixed state without opening an event stream. */
export function StaticStore({ state, children }: { state: ViewState; children: ReactNode }) {
  return <StoreContext.Provider value={{ state, dispatch: () => undefined }}>{children}</StoreContext.Provider>;
}

export function useStore(): Store {
  const store = useContext(StoreContext);
  if (store === null) throw new Error("useStore must be used inside a store provider");
  return store;
}
