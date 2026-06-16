import { useId } from "react";
import { Area, AreaChart, ResponsiveContainer, YAxis } from "recharts";

export interface SparkProps {
  data: number[];
  /** A CSS color or var, e.g. "var(--accent)". Defaults to the emerald accent. */
  tone?: string;
  height?: number;
}

/**
 * Tiny recharts area sparkline. No axes, no tooltip, no grid — pure trend.
 * A subtle vertical gradient fill under a 1.5px stroke.
 */
export function Spark({ data, tone = "var(--accent)", height = 28 }: SparkProps) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  const series = data.map((v, i) => ({ i, v }));

  if (data.length === 0) {
    return <div style={{ height }} aria-hidden />;
  }

  return (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={series} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={`spark-${id}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={tone} stopOpacity={0.28} />
              <stop offset="100%" stopColor={tone} stopOpacity={0} />
            </linearGradient>
          </defs>
          <YAxis hide domain={["dataMin", "dataMax"]} />
          <Area
            type="monotone"
            dataKey="v"
            stroke={tone}
            strokeWidth={1.5}
            fill={`url(#spark-${id})`}
            isAnimationActive={false}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export default Spark;
