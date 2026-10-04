import { describe, expect, it } from 'vitest';

import {
  isAssessable,
  MANDATORY_DISCLAIMER,
  PREFERENCE_LABEL,
  requiresDisclaimer,
  SENIORITY_LABEL,
  type FitAssessment,
  type PreferenceState,
  type SeniorityOutcome,
} from './match';

describe('semantic vocabulary', () => {
  it('covers every preference state exactly once', () => {
    const states: PreferenceState[] = [
      'aligned',
      'partially_aligned',
      'indeterminate',
      'not_evidenced',
      'negatively_indicated',
    ];
    expect(Object.keys(PREFERENCE_LABEL).sort()).toEqual([...states].sort());
  });

  it('covers every seniority outcome exactly once', () => {
    const outcomes: SeniorityOutcome[] = [
      'at_level',
      'below',
      'above',
      'different_track',
      'unknown',
      'indeterminate',
    ];
    expect(Object.keys(SENIORITY_LABEL).sort()).toEqual([...outcomes].sort());
  });

  // The frontend must not reinterpret backend semantics. These words
  // would turn an open-world absence into a negative claim.
  it('never labels not_evidenced as a negative', () => {
    const forbidden = ['missing', 'not supported', 'unsupported', 'unavailable', 'rejected', 'incompatible'];
    const label = PREFERENCE_LABEL.not_evidenced.toLowerCase();
    for (const word of forbidden) {
      expect(label).not.toContain(word);
    }
  });

  it('never labels indeterminate as a gap', () => {
    const label = PREFERENCE_LABEL.indeterminate.toLowerCase();
    expect(label).not.toContain('missing');
    expect(label).not.toContain('gap');
  });
});

describe('mandatory disclaimers', () => {
  it('requires a disclaimer for indeterminate and not_evidenced', () => {
    expect(requiresDisclaimer('indeterminate')).toBe(true);
    expect(requiresDisclaimer('not_evidenced')).toBe(true);
    expect(requiresDisclaimer('aligned')).toBe(false);
  });

  it('states that indeterminate is not counted against the user', () => {
    expect(MANDATORY_DISCLAIMER.indeterminate).toContain('not counted against you');
  });

  it('states that absence does not mean exclusion', () => {
    expect(MANDATORY_DISCLAIMER.not_evidenced).toContain('does not mean');
  });
});

describe('zero coverage', () => {
  it('treats a zero-coverage result as unassessable rather than a zero score', () => {
    const fit: FitAssessment = {
      matchScore: null,
      matchCategory: null,
      fitCoverage: { mandatory: 0, preferred: 0 },
      evaluatedRequirementCount: 0,
      totalRequirementCount: 7,
    };
    expect(isAssessable(fit)).toBe(false);
    expect(fit.matchScore).toBeNull();
    expect(fit.matchCategory).toBeNull();
  });
});
