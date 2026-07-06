import { describe, it, expect } from 'vitest';
import {
  stepRepeatDelay,
  parseOffsetInput,
} from '../../pikaraoke/static/js/offset-stepper.js';

describe('stepRepeatDelay', () => {
  it('waits longest before the first repeat', () => {
    expect(stepRepeatDelay(0)).toBeGreaterThan(stepRepeatDelay(1));
  });

  it('accelerates monotonically', () => {
    let prev = stepRepeatDelay(0);
    for (let i = 1; i < 20; i++) {
      const d = stepRepeatDelay(i);
      expect(d).toBeLessThanOrEqual(prev);
      prev = d;
    }
  });

  it('traverses the full ±2s range (80 steps of 0.05) in under 3s of holding', () => {
    let total = 0;
    for (let i = 0; i < 80; i++) total += stepRepeatDelay(i);
    expect(total).toBeLessThan(3000);
  });
});

describe('parseOffsetInput', () => {
  it('parses plain decimals', () => {
    expect(parseOffsetInput('1.35')).toBeCloseTo(1.35);
    expect(parseOffsetInput('0')).toBe(0);
    expect(parseOffsetInput(' 2 ')).toBe(2);
  });

  it('parses negative values', () => {
    expect(parseOffsetInput('-1.35')).toBeCloseTo(-1.35);
    expect(parseOffsetInput('-.5')).toBeCloseTo(-0.5);
  });

  it('accepts comma as decimal separator', () => {
    expect(parseOffsetInput('1,35')).toBeCloseTo(1.35);
    expect(parseOffsetInput('-0,2')).toBeCloseTo(-0.2);
  });

  it('accepts trailing minus (iOS keyboard has no minus key)', () => {
    expect(parseOffsetInput('2-')).toBe(-2);
    expect(parseOffsetInput('1.35-')).toBeCloseTo(-1.35);
    expect(parseOffsetInput('0,5-')).toBeCloseTo(-0.5);
  });

  it('rejects garbage', () => {
    expect(parseOffsetInput('abc')).toBeNaN();
    expect(parseOffsetInput('1.2.3')).toBeNaN();
    expect(parseOffsetInput('--1')).toBeNaN();
    expect(parseOffsetInput('1--')).toBeNaN();
    expect(parseOffsetInput('')).toBeNaN();
    expect(parseOffsetInput(null)).toBeNaN();
    expect(parseOffsetInput('-')).toBeNaN();
  });
});
