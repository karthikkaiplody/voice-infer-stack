import layout from "./layout.module.css";

/**
 * Shown while the agent is listening but its input is exactly silent. The page
 * cannot name the microphone (device names are never sent to it), so this says
 * where to look and what to do, in the order that usually works.
 */
export function InputSilentNotice() {
  return (
    <div className={layout.notice} role="alert">
      <p>
        <strong>No sound is reaching the agent.</strong> The microphone it opened is delivering
        silence. Usually your Mac's selected input is a virtual device (a mixer or loopback) or not
        the microphone you are talking into.
      </p>
      <ol>
        <li>
          Open <strong>System Settings → Sound → Input</strong> and select your real microphone.
          Speak, and its input level bar should move.
        </li>
        <li>
          Press <strong>Stop listening</strong>, then stop <code>make live</code> (Ctrl+C) and run it
          again, so the agent opens the microphone you just selected. This page reconnects by itself.
        </li>
        <li>
          Still silent? <code>make devices</code> lists every input with a number. Pick one with{" "}
          <code>VOICE_AUDIO_DEVICE=&lt;number&gt; make live</code>.
        </li>
      </ol>
    </div>
  );
}
