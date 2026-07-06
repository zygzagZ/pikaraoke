/**
 * Pure helpers for the subtitle-offset stepper: hold-to-repeat schedule and
 * tolerant manual-entry parsing (US-56).
 *
 * Consumed by now-playing-bar.js via window.PK.OffsetStepper; exported as an
 * ES module for vitest.
 */

/**
 * Delay (ms) before the next auto-repeat step while a stepper button is held.
 * repeatIndex 0 is the gap between the press and the first repeated step.
 *
 * The schedule must traverse the full ±2s range (80 steps of 0.05) in about
 * two seconds of holding, while the warm-up stays slow enough to release
 * after one or two extra steps deliberately.
 */
export function stepRepeatDelay(repeatIndex) {
  if (repeatIndex === 0) return 350;
  if (repeatIndex <= 6) return 120;
  return 25;
}

/**
 * Parse manual offset entry. Accepts comma decimals ("1,35") and a trailing
 * minus ("2-" means -2) because the iOS decimal keyboard has no minus key.
 * Returns NaN when the text is not a plain number.
 */
export function parseOffsetInput(raw) {
  if (raw == null) return NaN;
  let s = String(raw).trim().replace(',', '.');
  if (/^\d*\.?\d+-$/.test(s)) s = '-' + s.slice(0, -1);
  if (!/^-?\d*\.?\d+$/.test(s)) return NaN;
  return parseFloat(s);
}

if (typeof window !== 'undefined') {
  window.PK = window.PK || {};
  window.PK.OffsetStepper = { stepRepeatDelay, parseOffsetInput };
}
