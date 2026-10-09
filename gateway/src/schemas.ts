/**
 * Public request contract owned by the gateway (TypeBox / JSON Schema).
 *
 * These schemas mirror the Pydantic request models in agent/schemas.py. The
 * shared fixtures in contracts/fixtures/plan_requests.json are run against both
 * implementations so the two contracts cannot drift silently. Cross-field rules
 * (for example min <= max temperature) are enforced by the Python service.
 */
import Type, { type Static } from "typebox";

const SINGLE_LINE = "^[^\\x00-\\x1f\\x7f]*[^\\x00-\\x20\\x7f][^\\x00-\\x1f\\x7f]*$";
const MULTILINE = "^[^\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f]*$";
export const SESSION_ID_PATTERN = "^[A-Za-z0-9_-]{16,128}$";
export const PLAN_ID_PATTERN = "^[A-Za-z0-9._-]{8,64}$";
const PLAN_HASH_PATTERN = "^[0-9a-f]{64}$";

const SessionId = Type.String({ pattern: SESSION_ID_PATTERN, description: "Server-generated Gradio session id" });

export const Tone = Type.Union([
  Type.Literal("Friendly"),
  Type.Literal("Urgent"),
  Type.Literal("Playful"),
  Type.Literal("Premium"),
]);

export const ClientId = Type.Union([
  Type.Literal("general"),
  Type.Literal("demo_coffee_shop"),
  Type.Literal("demo_outdoor_fitness"),
]);

const Weekday = Type.Union(
  ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day) => Type.Literal(day)),
);

const StartHour = Type.Integer({ minimum: 0, maximum: 23 });
const EndHour = Type.Integer({ minimum: 1, maximum: 24 });

const NullableNumber = (minimum: number, maximum: number) =>
  Type.Optional(Type.Union([Type.Number({ minimum, maximum }), Type.Null()]));

export const HourRange = Type.Object(
  { start_hour: StartHour, end_hour: EndHour },
  { additionalProperties: false },
);

export const ExclusionWindow = Type.Object(
  {
    weekdays: Type.Array(Weekday, { minItems: 1, maxItems: 7 }),
    start_hour: StartHour,
    end_hour: EndHour,
    reason: Type.Optional(Type.String({ maxLength: 120 })),
  },
  { additionalProperties: false },
);

export const ClientConstraints = Type.Object(
  {
    allowed_hours: Type.Optional(Type.Array(HourRange, { maxItems: 6 })),
    min_temperature_f: NullableNumber(-60, 140),
    max_temperature_f: NullableNumber(-60, 140),
    max_precipitation_probability_pct: NullableNumber(0, 100),
    max_wind_speed_mph: NullableNumber(0, 150),
    max_aqi: Type.Optional(Type.Union([Type.Integer({ minimum: 1, maximum: 5 }), Type.Null()])),
    exclusion_windows: Type.Optional(Type.Array(ExclusionWindow, { maxItems: 14 })),
  },
  { additionalProperties: false },
);

export const CampaignBrief = Type.Object(
  {
    location: Type.String({ minLength: 1, maxLength: 120, pattern: SINGLE_LINE }),
    business_type: Type.String({ minLength: 1, maxLength: 80, pattern: SINGLE_LINE }),
    campaign_goal: Type.Optional(Type.String({ maxLength: 300, pattern: MULTILINE })),
    tone: Type.Optional(Tone),
  },
  { additionalProperties: false },
);

export const CampaignPlanRequest = Type.Object(
  {
    session_id: SessionId,
    brief: CampaignBrief,
    client_id: Type.Optional(ClientId),
    constraints: Type.Optional(Type.Union([ClientConstraints, Type.Null()])),
  },
  { additionalProperties: false },
);

export const ReviewRequest = Type.Object(
  {
    session_id: SessionId,
    decision: Type.Union([Type.Literal("approve"), Type.Literal("reject")]),
    plan_hash: Type.String({ pattern: PLAN_HASH_PATTERN }),
    note: Type.Optional(Type.String({ maxLength: 500, pattern: MULTILINE })),
  },
  { additionalProperties: false },
);

export const RevisionRequest = Type.Object(
  {
    session_id: SessionId,
    base_plan_hash: Type.String({ pattern: PLAN_HASH_PATTERN }),
    ad_copy: Type.Record(Type.String(), Type.Array(Type.String()), { minProperties: 1, maxProperties: 3 }),
    note: Type.Optional(Type.String({ maxLength: 500, pattern: MULTILINE })),
  },
  { additionalProperties: false },
);

export const PlanParams = Type.Object(
  { requestId: Type.String({ pattern: PLAN_ID_PATTERN }) },
  { additionalProperties: false },
);

export const ErrorEnvelope = Type.Object({
  error: Type.Object({
    code: Type.String(),
    message: Type.String(),
    request_id: Type.Union([Type.String(), Type.Null()]),
    retryable: Type.Boolean(),
    details: Type.Array(Type.Object({ path: Type.String(), message: Type.String() })),
  }),
});

export type CampaignPlanRequestBody = Static<typeof CampaignPlanRequest>;
export type ReviewRequestBody = Static<typeof ReviewRequest>;
export type RevisionRequestBody = Static<typeof RevisionRequest>;
export type PlanParamsType = Static<typeof PlanParams>;
export type ErrorEnvelopeBody = Static<typeof ErrorEnvelope>;
