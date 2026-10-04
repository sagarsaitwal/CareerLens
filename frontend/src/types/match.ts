/**
 * Semantic vocabulary mirrored from the backend.
 *
 * The frontend must NOT reinterpret backend semantics. "Not evidenced"
 * is not "Missing" and not "Not supported" — those would convert an
 * open-world absence into a negative claim, which the whole preference
 * axis exists to prevent (DATABASE.md Section 3.5).
 *
 * These unions and the label maps below are the single place where
 * backend states become user-facing words.
 */

export type PreferenceState =
  | 'aligned'
  | 'partially_aligned'
  | 'indeterminate'
  | 'not_evidenced'
  | 'negatively_indicated';

export type GapType = 'missing' | 'below-threshold-experience';

export type SeniorityOutcome =
  | 'at_level'
  | 'below'
  | 'above'
  | 'different_track'
  | 'unknown'
  | 'indeterminate';

export type ResolutionProvenance =
  | 'exact_match'
  | 'normalized'
  | 'alias_rule'
  | 'user_confirmed'
  | 'ai_semantic'
  | 'unresolved';

export type EvidenceSource =
  | 'structured_field'
  | 'title'
  | 'requirements'
  | 'responsibilities'
  | 'description';

export const PREFERENCE_LABEL: Record<PreferenceState, string> = {
  aligned: 'Aligned',
  partially_aligned: 'Partially aligned',
  indeterminate: 'Could not determine',
  not_evidenced: 'Not mentioned in this posting',
  negatively_indicated: 'Posting indicates otherwise',
};

export const SENIORITY_LABEL: Record<SeniorityOutcome, string> = {
  at_level: 'At your level',
  below: 'Below',
  above: 'Above',
  different_track: 'Different track',
  unknown: 'Not stated',
  indeterminate: 'Could not determine',
};

/**
 * Mandatory disclaimers (AI-MATCHING.md Section 9).
 *
 * These are invariants, not copy suggestions: without them the UI
 * silently converts uncertainty into a negative. Rendering an
 * indeterminate or not-evidenced state without its disclaimer is a bug.
 */
export const MANDATORY_DISCLAIMER = {
  indeterminate: 'This is not counted against you.',
  not_evidenced: 'That does not mean the role excludes it.',
} as const;

export type DisclaimerState = keyof typeof MANDATORY_DISCLAIMER;

export function requiresDisclaimer(state: string): state is DisclaimerState {
  return state in MANDATORY_DISCLAIMER;
}

/**
 * A match score is meaningless without its coverage, and both are null
 * when nothing could be assessed. Modelling them as one object makes it
 * impossible to render a score alone.
 */
export interface FitAssessment {
  matchScore: number | null;
  matchCategory: string | null;
  fitCoverage: {
    mandatory: number;
    preferred: number;
  };
  evaluatedRequirementCount: number;
  totalRequirementCount: number;
}

export function isAssessable(fit: FitAssessment): boolean {
  return fit.evaluatedRequirementCount > 0;
}
