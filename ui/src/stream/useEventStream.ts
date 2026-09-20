import { useEffect, type Dispatch } from "react";
import { parseServerMessage } from "../contract/events";
import type { Action } from "../state/reducer";

/**
 * Subscribe to the local server's event stream (Server-Sent Events).
 *
 * Every payload is parsed and validated before it reaches state; anything that
 * is not valid JSON or not a message the UI accepts is dropped. The browser
 * reconnects on its own, and the server sends a fresh `hello` when it does.
 */
export function useEventStream(dispatch: Dispatch<Action>): void {
  useEffect(() => {
    const source = new EventSource("/events");
    source.onopen = () => dispatch({ type: "connection", status: "open" });
    source.onerror = () => dispatch({ type: "connection", status: "reconnecting" });
    source.onmessage = (message: MessageEvent<string>) => {
      let payload: unknown;
      try {
        payload = JSON.parse(message.data);
      } catch {
        return;
      }
      const parsed = parseServerMessage(payload);
      if (parsed !== null) dispatch({ type: "message", message: parsed });
    };
    return () => source.close();
  }, [dispatch]);
}
