import type { Stage } from "../api/types";

export default function Stepper({ stage, finished }: { stage: Stage; finished: boolean }) {
  return (
    <ol className="stepper" aria-label="Progress">
      {stage.stages.map((s, i) => {
        const done = i < stage.index || (finished && i <= stage.index);
        const current = !finished && i === stage.index;
        return (
          <li key={s.key} className={`step ${done ? "done" : ""} ${current ? "current" : ""}`} aria-current={current ? "step" : undefined}>
            {s.label}
          </li>
        );
      })}
    </ol>
  );
}
