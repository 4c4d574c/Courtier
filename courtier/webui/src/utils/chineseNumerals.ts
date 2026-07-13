// NOTE: Caps at 10, falling back to Arabic numerals.  Most audit workflows
// complete in well under 10 steps; if longer workflows become common, extend
// the array or add compound-numeral generation.
const CHINESE_NUMERALS = [
  "壹",
  "贰",
  "叁",
  "肆",
  "伍",
  "陆",
  "柒",
  "捌",
  "玖",
  "拾",
];

export function toChineseNumeral(n: number): string {
  if (n > 0 && n <= CHINESE_NUMERALS.length) return CHINESE_NUMERALS[n - 1];
  return String(n);
}
