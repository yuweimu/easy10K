/** Choose regular calendar intervals, anchored to the highest-priority quarter end. */
export function trendDateLabelIndices(dates: string[], columnSpacing: number): Set<number> {
  if (!dates.length) return new Set();
  const quarters = dates.map((date) => Number(date.slice(0, 4)) * 4 + Math.floor((Number(date.slice(5, 7)) - 1) / 3));
  const preferred = ["12-31", "06-30", "09-30", "03-31"];
  let anchor = dates.length - 1;
  for (const ending of preferred) {
    const index = dates.map((date) => date.slice(5)).lastIndexOf(ending);
    if (index >= 0) {
      anchor = index;
      break;
    }
  }
  const span = quarters[quarters.length - 1] - quarters[0];
  // Quarterly, half-yearly, yearly, then every two, three, ... years.
  for (let interval = 1; interval <= Math.max(4, span + 4); interval = interval < 4 ? interval * 2 : interval + 4) {
    const indices = quarters.flatMap((quarter, index) =>
      (quarter - quarters[anchor]) % interval === 0 ? [index] : []);
    if (indices.every((index, position) => position === 0 ||
      (index - indices[position - 1]) * columnSpacing >= 90)) {
      return new Set(indices);
    }
  }
  return new Set([anchor]);
}
