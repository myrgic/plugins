// types/index.d.ts — cogos-harness's state contract (plan §2, §3).
//
// One owner per key, prefixed by mod. Each mod agent edits ONLY its own
// block; the shared block belongs to the skeleton PR (plan §4).
// No import or reference here: plugin contracts are self-contained.

export type CogosHarnessMode = "off" | "shadow" | "on";
export type CogosHarnessModName = "redact" | "guard" | "context" | "status" | "receipt";

export type CogosHarnessCfg = {
  version: number;
  source: "file" | "default-off";
  mods: Record<CogosHarnessModName, CogosHarnessMode>;
  raw: Record<string, unknown>;
  loaded_at: number;
};

// §3.3 — written by cog-status only; read by cog-context and out-of-process
// readers (mirrored to ~/.cog/run/vitals.json).
export type CogosHarnessVitals = {
  written_at: number;
  kernel?: { reachable: boolean; version?: string; state?: string };
  usage?: { context_pct?: number; five_h_pct?: number; week_pct?: number };
  disk?: { free_gb?: number; low?: boolean };
  git?: { branch?: string; dirty?: number; ahead?: number };
  prs?: { open?: number; stale?: number };
  hook_health?: Record<string, unknown>;
};

declare module "claude-code" {
  interface PluginState {
    "cogos-harness": {
      // ── shared (owner: skeleton / lib/cfg.mjs) ─────────────────────────
      cfg: CogosHarnessCfg;

      // ── cog-redact (owner: cog-redact) ─────────────────────────────────
      // "redact.map": placeholder → hash mapping, added by the redact PR.

      // ── cog-guard (owner: cog-guard) ───────────────────────────────────

      // ── cog-context (owner: cog-context) ───────────────────────────────

      // ── cog-status (owner: cog-status) ─────────────────────────────────
      "status.vitals": CogosHarnessVitals;

      // ── cog-receipt (owner: cog-receipt) ───────────────────────────────
    };
  }
}
