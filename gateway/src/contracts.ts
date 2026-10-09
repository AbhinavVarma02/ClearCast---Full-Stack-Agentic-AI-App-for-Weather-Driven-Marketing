/**
 * Response contracts generated from the Python Pydantic models
 * (python -m api.export_contracts). The gateway validates every successful
 * upstream response against them before returning it to clients.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Ajv2020, type ErrorObject, type ValidateFunction } from "ajv/dist/2020.js";
import * as ajvFormats from "ajv-formats";

type FormatsPlugin = (ajv: Ajv2020) => Ajv2020;
// ajv-formats is CommonJS; under NodeNext the callable may sit on `default`.
const addFormats: FormatsPlugin =
  ((ajvFormats as unknown as { default?: FormatsPlugin }).default ?? (ajvFormats as unknown as FormatsPlugin));

export type ContractName =
  | "campaign_plan_request"
  | "campaign_plan_response"
  | "review_request"
  | "revision_request"
  | "error_response";

export function contractsDir(): string {
  const configured = process.env.CLEARCAST_CONTRACTS_DIR?.trim();
  if (configured) {
    return configured;
  }
  // src/ and dist/ both sit one level below gateway/, which sits beside contracts/.
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "contracts");
}

export function loadContract(name: ContractName, dir: string = contractsDir()): Record<string, unknown> {
  return JSON.parse(readFileSync(path.join(dir, `${name}.schema.json`), "utf8")) as Record<string, unknown>;
}

export type ValidationResult = { ok: true } | { ok: false; errors: string[] };

export interface ContractValidator {
  validate(data: unknown): ValidationResult;
}

function describe(errors: ErrorObject[] | null | undefined): string[] {
  return (errors ?? []).slice(0, 10).map((error) => `${error.instancePath || "/"} ${error.message ?? "invalid"}`);
}

export function createContractValidator(name: ContractName, dir: string = contractsDir()): ContractValidator {
  const ajv = new Ajv2020({ allErrors: true, strict: false });
  addFormats(ajv);
  const compiled: ValidateFunction = ajv.compile(loadContract(name, dir));
  return {
    validate(data: unknown): ValidationResult {
      return compiled(data) ? { ok: true } : { ok: false, errors: describe(compiled.errors) };
    },
  };
}
