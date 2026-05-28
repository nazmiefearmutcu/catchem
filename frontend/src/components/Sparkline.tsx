/**
 * Tiny SVG polyline normalized within its own [min,max] so even a low-variance
 * series shows visible motion. Renders nothing for <2 points so we never claim
 * a "trend" exists when there isn't one.
 *
 * Extracted from BenchmarkPage so DeepSeek spend history + future widgets can
 * reuse the same shape (avoids duplicate sparkline implementations).
 */
export function Sparkline({
  points,
  className = "",
  width = 56,
  height = 14,
  strokeWidth = 1.25,
  opacity = 0.7,
  ariaLabel,
}: {
  points: number[];
  className?: string;
  width?: number;
  height?: number;
  strokeWidth?: number;
  opacity?: number;
  ariaLabel?: string;
}) {
  if (points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const step = width / (points.length - 1);
  const d = points
    .map(
      (v, i) =>
        `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(height - ((v - min) / range) * height).toFixed(1)}`,
    )
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      className={className}
      aria-hidden={ariaLabel ? undefined : true}
      aria-label={ariaLabel}
      role={ariaLabel ? "img" : undefined}
    >
      <path
        d={d}
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity={opacity}
      />
    </svg>
  );
}
