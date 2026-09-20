import { useStore } from "../state/store";
import { ModeSwitch } from "./ModeSwitch";
import styles from "./layout.module.css";

const CONNECTION = {
  connecting: "Connecting…",
  open: "Connected to local server",
  reconnecting: "Reconnecting…",
} as const;

export function Header() {
  const { state } = useStore();
  const agent = state.hello?.agent;
  return (
    <header className={styles.header}>
      <div>
        <p className={styles.eyebrow}>Local · metadata only</p>
        <h1 className={styles.title}>Voice agent trace</h1>
      </div>
      <div className={styles.badges}>
        <ModeSwitch />
        {agent != null && <span className={styles.badge}>Agent: {agent.title}</span>}
        <span className={styles.badge}>{CONNECTION[state.connection]}</span>
      </div>
    </header>
  );
}
