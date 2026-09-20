import { ConfigSummary } from "./components/ConfigSummary";
import { CurrentState } from "./components/CurrentState";
import { EventLog } from "./components/EventLog";
import { Header } from "./components/Header";
import { LiveControls } from "./components/LiveControls";
import { OutcomeCard } from "./components/OutcomeCard";
import { PrimaryMetric } from "./components/PrimaryMetric";
import { SyntheticBanner } from "./components/SyntheticBanner";
import { Waterfall } from "./components/Waterfall";
import { ScenarioControls } from "./components/ScenarioControls";
import { TuningPanel } from "./components/TuningPanel";
import layout from "./components/layout.module.css";
import { useStore } from "./state/store";

export function App() {
  const { state } = useStore();
  return (
    <main className={layout.page}>
      <Header />
      <SyntheticBanner />
      <ScenarioControls />
      <LiveControls />
      <TuningPanel />
      {state.notice !== null && <p className={layout.notice} role="alert">{state.notice}</p>}
      {state.inputSilent && state.runtime === "listening" && (
        <p className={layout.notice} role="alert">
          No sound is reaching the agent: the microphone is delivering silence. Your system's default
          input may be a virtual device. Stop, run <code>make devices</code>, and start again with{" "}
          <code>VOICE_AUDIO_DEVICE=&lt;number&gt; make live</code>.
        </p>
      )}
      <section className={layout.hero} aria-label="Current turn">
        <CurrentState />
        <PrimaryMetric />
        <OutcomeCard />
      </section>
      <Waterfall />
      <EventLog />
      <ConfigSummary />
      <p className={layout.footer}>
        Every value on this page comes from the repository's telemetry contract v1, through its
        metadata-only privacy filter. A stage this source cannot report is never shown as a time.
      </p>
    </main>
  );
}
