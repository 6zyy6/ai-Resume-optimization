import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(import.meta.dirname, "..", "..");
const read = (path: string) => readFileSync(join(root, path), "utf8");
const json = <T>(path: string): T => JSON.parse(read(path)) as T;

type Manifest = {
  spec: {
    image?: string;
    replicas?: { min: number; max: number };
    concurrency?: number;
    network?: { ingress: string; egress: string };
    envFromSecrets?: string[];
    health?: Record<string, string>;
    rollout?: { canaryPercent: number; observationMinutes: number };
    groups?: Array<{ name: string; queues: string[]; concurrency: number }>;
  };
};

type Compose = {
  services: Record<string, {
    image?: string;
    ports?: string[];
    networks?: string[];
    healthcheck?: unknown;
    command?: string[];
    environment?: Record<string, string>;
  }>;
  networks: Record<string, { internal?: boolean }>;
};

describe("deployment contract", () => {
  const api = json<Manifest>("infra/deployment/cloudbase/api.yaml");
  const ai = json<Manifest>("infra/deployment/cloudbase/ai.yaml");
  const web = json<Manifest>("infra/deployment/cloudbase/web.yaml");
  const workers = json<Manifest>("infra/deployment/cloudbase/workers.yaml");
  const compose = json<Compose>("infra/docker/docker-compose.yml");

  it("keeps PostgreSQL and Redis private in local and cloud topology", () => {
    expect(compose.services.postgres.ports).toBeUndefined();
    expect(compose.services.redis.ports).toBeUndefined();
    expect(compose.networks.data.internal).toBe(true);
    expect(api.spec.network?.egress).toBe("vpc");
    expect(workers.spec.network?.ingress).toBe("none");
  });

  it("uses immutable images and environment-only secrets", () => {
    for (const manifest of [api, ai, web, workers]) {
      expect(manifest.spec.image).toContain("${COMMIT_SHA}");
      expect(manifest.spec.image).not.toMatch(/:latest$/);
    }
    const deploymentText = [
      "infra/deployment/cloudbase/api.yaml",
      "infra/deployment/cloudbase/ai.yaml",
      "infra/deployment/cloudbase/web.yaml",
      "infra/deployment/cloudbase/workers.yaml",
      "infra/docker/docker-compose.yml",
    ].map(read).join("\n");
    expect(deploymentText).not.toMatch(/(sk-[A-Za-z0-9]|BEGIN PRIVATE KEY|password["']?\s*:\s*["'][^$])/i);
  });

  it("provides required replica and concurrency baselines", () => {
    expect(api.spec.replicas?.min).toBeGreaterThanOrEqual(2);
    expect(ai.spec.concurrency).toBe(10);
    const groups = Object.fromEntries((workers.spec.groups ?? []).map((group) => [group.name, group]));
    expect(groups["ai-worker"].concurrency).toBe(10);
    expect(groups["file-worker"].concurrency).toBe(5);
  });

  it("declares exactly the fixed production queues", () => {
    const queues = (workers.spec.groups ?? []).flatMap((group) => group.queues).sort();
    expect(queues).toEqual(["ai.batch", "ai.interactive", "file.export", "file.parse", "privacy"]);
  });

  it("exposes health and version probes and a 10 percent 30 minute canary", () => {
    for (const manifest of [api, ai, web]) {
      expect(manifest.spec.health).toEqual(expect.objectContaining({
        live: expect.any(String),
        ready: expect.any(String),
        version: expect.any(String),
      }));
      expect(manifest.spec.rollout).toEqual(expect.objectContaining({
        canaryPercent: 10,
        observationMinutes: 30,
      }));
    }
    for (const service of ["postgres", "redis", "api", "ai", "web", "otel-collector"]) {
      expect(compose.services[service].healthcheck).toBeDefined();
    }
  });

  it("implements the version routes named by deployment probes", () => {
    expect(read("packages/api/app/main.py")).toContain('"/v1/version"');
    expect(read("packages/ai/src/server/app.ts")).toContain('"/internal/v1/version"');
    expect(read("app/web/app/version/route.ts")).toContain("APP_COMMIT_SHA");
  });

  it("keeps AI and file workers independently scalable with graceful shutdown", () => {
    const text = read("infra/deployment/cloudbase/workers.yaml");
    expect(text).toContain('"gracefulShutdownSeconds": 45');
    expect(workers.spec.groups?.map((group) => group.name)).toEqual([
      "ai-worker",
      "file-worker",
      "privacy-worker",
    ]);
  });

  it("defines multi-stage service images without embedding runtime credentials", () => {
    for (const path of [
      "infra/docker/api.Dockerfile",
      "infra/docker/ai.Dockerfile",
      "infra/docker/web.Dockerfile",
    ]) {
      const dockerfile = read(path);
      expect(dockerfile).toContain("FROM ");
      expect(dockerfile).not.toMatch(/(OPENAI_API_KEY|COS_SECRET_KEY|DATABASE_URL)=\S+/);
    }
  });
});
