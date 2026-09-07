// @vitest-environment jsdom
/**
 * Behavioral coverage for the kanban dashboard plugin's host quota-circuit
 * banner. The bundle is a plain IIFE that registers its page on
 * ``window.__HERMES_PLUGINS__`` and reads React + REST helpers off
 * ``window.__HERMES_PLUGIN_SDK__``; this test supplies both, mounts the
 * banner in jsdom, and drives the rendered DOM — no source-string
 * inspection. The contract pinned here:
 *
 *   - the banner stays unmounted while ``GET quota-circuits`` reports no
 *     circuits, and renders one row per circuit with the sanitized handle,
 *     reason, timestamps and deferred counts once one is active;
 *   - the **Clear circuit** button issues ``DELETE quota-circuits/<handle>``
 *     for exactly that handle and refreshes, so the banner disappears when
 *     the backend reports the circuit gone.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, it } from "vitest";

const BUNDLE = path.resolve(
  __dirname,
  "../../../plugins/kanban/dashboard/dist/index.js",
);

type Circuit = {
  group: string;
  state: string;
  reason: string;
  first_observed_at: number;
  last_observed_at: number;
  next_eligible_at: number;
  observations: number;
  resume_probe_at: number | null;
  boards_deferred: number;
  cards_deferred: number;
};

interface FakeSdkState {
  circuits: Circuit[];
  calls: Array<{ url: string; method: string }>;
}

function passthrough(props: Record<string, unknown> & { children?: React.ReactNode }) {
  return React.createElement("div", null, props.children);
}

function installFakeSdk(state: FakeSdkState) {
  const win = window as unknown as Record<string, unknown>;
  const registered: Record<string, unknown> = {};
  win.__HERMES_PLUGINS__ = {
    register(name: string, component: unknown) {
      registered[name] = component;
    },
  };
  win.__HERMES_PLUGIN_SDK__ = {
    React,
    components: {
      Card: passthrough, CardContent: passthrough, Badge: passthrough, Button: passthrough,
      Input: passthrough, Label: passthrough, Select: passthrough, SelectOption: passthrough,
    },
    hooks: {
      useState: React.useState, useEffect: React.useEffect, useCallback: React.useCallback,
      useMemo: React.useMemo, useRef: React.useRef,
    },
    utils: { cn: (...parts: unknown[]) => parts.filter(Boolean).join(" "), timeAgo: () => "" },
    fetchJSON(url: string, init?: { method?: string }) {
      const method = (init && init.method) || "GET";
      state.calls.push({ url, method });
      if (method === "DELETE") {
        const handle = decodeURIComponent(url.split("/quota-circuits/")[1] ?? "");
        const before = state.circuits.length;
        state.circuits = state.circuits.filter((c) => c.group !== handle);
        if (state.circuits.length === before) {
          return Promise.reject(new Error("no such circuit"));
        }
        return Promise.resolve({ cleared: true, group: handle });
      }
      return Promise.resolve({ active: state.circuits.length > 0, circuits: state.circuits });
    },
  };
  return registered;
}

function loadBanner(state: FakeSdkState): React.ComponentType {
  const registered = installFakeSdk(state);
  const source = fs.readFileSync(BUNDLE, "utf8");
  // The bundle is a browser IIFE: evaluate it against jsdom's window as the
  // global object, exactly as a <script> tag would.
  vm.runInContext(source, vm.createContext(window));
  const page = registered.kanban as { QuotaCircuitBanner?: React.ComponentType } | undefined;
  assert.ok(page, "bundle must register the kanban page");
  assert.ok(page.QuotaCircuitBanner, "kanban page must expose its quota banner");
  return page.QuotaCircuitBanner;
}

function flush() {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

const activeCircuit: Circuit = {
  group: "budget-3437a4820824",
  state: "paused",
  reason: "rate_limit",
  first_observed_at: 1_800_000_000,
  last_observed_at: 1_800_000_060,
  next_eligible_at: 1_800_000_600,
  observations: 2,
  resume_probe_at: null,
  boards_deferred: 2,
  cards_deferred: 5,
};

describe("kanban quota circuit banner", () => {
  it("stays unmounted while no host circuit is active", async () => {
    const state: FakeSdkState = { circuits: [], calls: [] };
    const Banner = loadBanner(state);
    await act(async () => root.render(React.createElement(Banner)));
    await flush();
    assert.equal(container.querySelector(".hermes-kanban-quota-circuits"), null);
    assert.deepEqual(state.calls.map((c) => c.method), ["GET"]);
  });

  it("renders sanitized diagnostics and clears the circuit through the API", async () => {
    const state: FakeSdkState = { circuits: [activeCircuit], calls: [] };
    const Banner = loadBanner(state);
    await act(async () => root.render(React.createElement(Banner)));
    await flush();

    const banner = container.querySelector(".hermes-kanban-quota-circuits");
    assert.ok(banner, "banner mounts once a circuit is active");
    const text = banner.textContent ?? "";
    assert.match(text, /budget-3437a4820824/);
    assert.match(text, /rate_limit/);
    assert.match(text, /Boards deferred 2/);
    assert.match(text, /Cards deferred 5/);
    assert.match(text, /Next eligible/);
    assert.equal(banner.querySelectorAll(".hermes-kanban-quota-row").length, 1);

    const button = banner.querySelector<HTMLButtonElement>("button.hermes-kanban-quota-clear");
    assert.ok(button, "each circuit row carries a Clear circuit control");
    assert.equal(button.textContent, "Clear circuit");
    await act(async () => {
      button.click();
    });
    await flush();

    const deletes = state.calls.filter((c) => c.method === "DELETE");
    assert.equal(deletes.length, 1);
    assert.equal(
      deletes[0].url,
      "/api/plugins/kanban/quota-circuits/" + encodeURIComponent(activeCircuit.group),
    );
    assert.equal(state.circuits.length, 0);
    // After the refresh the row is gone and the outcome is reported.
    assert.equal(container.querySelectorAll(".hermes-kanban-quota-row").length, 0);
    assert.match(container.textContent ?? "", /Quota circuit cleared/);
  });
});
