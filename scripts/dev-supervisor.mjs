import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

function exitCode(code, signal) {
  return code ?? (signal === "SIGINT" ? 130 : signal === "SIGTERM" ? 143 : 1);
}

function waitForClose(child) {
  return new Promise((resolve) => child.once("close", resolve));
}

function safeToken(value, fallback) {
  const normalized = String(value ?? "")
    .replace(/[^a-z0-9_.-]/gi, "_")
    .slice(0, 64);
  return normalized || fallback;
}

function processErrorCode(code, signal) {
  if (code !== null && code !== undefined) return `process_exit_${code}`;
  return `process_signal_${safeToken(signal, "unknown").toLowerCase()}`;
}

async function responseJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function isServiceReady(spec, fetchReady) {
  try {
    const response = await fetchReady(spec.readyUrl);
    if (!response.ok) return false;
    const body = await responseJson(response);
    if (
      spec.expectedStatus
      && (typeof body !== "object" || body?.status !== spec.expectedStatus)
    ) return false;
    if (!spec.expectedService) return true;
    const identity = spec.identityUrl && spec.identityUrl !== spec.readyUrl
      ? await fetchReady(spec.identityUrl)
      : response;
    if (!identity.ok) return false;
    const identityBody = identity === response ? body : await responseJson(identity);
    return (
      typeof identityBody === "object"
      && identityBody?.service === spec.expectedService
    );
  } catch {
    return false;
  }
}

export function runDevSupervisor(commands, options = {}) {
  return new Promise((resolve) => {
    const fetchReady = options.fetch ?? globalThis.fetch;
    const intervalMs = options.intervalMs ?? 100;
    const log = options.log ?? console.log;
    const timeoutMs = options.timeoutMs ?? 30_000;
    const processes = commands.map((spec) => ({
      spec,
      child: spawn(spec.command, spec.args, { cwd: spec.cwd, stdio: "inherit" }),
    }));
    let finished = false;
    let readinessTimer;

    const removeSignalHandlers = () => {
      process.off("SIGINT", onSigint);
      process.off("SIGTERM", onSigterm);
    };

    const finish = (origin, code, signal) => {
      if (finished) return;
      finished = true;
      clearTimeout(readinessTimer);
      removeSignalHandlers();
      const siblings = processes
        .map(({ child }) => child)
        .filter((child) => child !== origin && child.exitCode === null);
      const closed = siblings.map(waitForClose);
      for (const child of siblings) child.kill(signal ?? "SIGTERM");
      const forceKill = setTimeout(() => {
        for (const child of siblings) {
          if (child.exitCode === null) child.kill("SIGKILL");
        }
      }, 5000);
      forceKill.unref();
      Promise.all(closed).then(() => {
        clearTimeout(forceKill);
        resolve(exitCode(code, signal));
      });
    };

    const onSigint = () => finish(undefined, null, "SIGINT");
    const onSigterm = () => finish(undefined, null, "SIGTERM");

    process.once("SIGINT", onSigint);
    process.once("SIGTERM", onSigterm);
    for (const { child, spec } of processes) {
      const service = safeToken(spec.name, "unknown");
      child.once("error", (error) => {
        const errorName = safeToken(error?.code, "unknown").toLowerCase();
        log(`[dev] service=${service} status=failed error_code=spawn_error_${errorName}`);
        finish(child, 1);
      });
      child.once("exit", (code, signal) => {
        if (!finished) {
          log(
            `[dev] service=${service} status=failed error_code=${processErrorCode(code, signal)}`,
          );
        }
        finish(child, code, signal);
      });
    }

    const readiness = commands.filter(({ readyUrl }) => readyUrl);
    if (readiness.length > 0) {
      const startedAt = Date.now();
      const checkReadiness = async () => {
        if (finished) return;
        const results = await Promise.all(
          readiness.map((spec) => isServiceReady(spec, fetchReady)),
        );
        if (results.every(Boolean)) {
          log(`[dev] status=ready services=${readiness.map(({ name }) => safeToken(name, "unknown")).join(",")}`);
          return;
        }
        if (Date.now() - startedAt >= timeoutMs) {
          log("[dev] service=supervisor status=failed error_code=readiness_timeout");
          finish(undefined, 1);
          return;
        }
        readinessTimer = setTimeout(checkReadiness, intervalMs);
      };
      void checkReadiness();
    }
  });
}

export function localDevCommands(root = process.cwd()) {
  const apiRoot = resolve(root, "packages/api");
  const python = resolve(root, ".venv/bin/python");
  return [
    {
      name: "web",
      expectedService: "web",
      identityUrl: "http://127.0.0.1:3000/version",
      readyUrl: "http://127.0.0.1:3000/version",
      command: "pnpm",
      args: ["--filter", "@resume/web", "dev"],
      cwd: root,
    },
    {
      name: "api",
      expectedService: "api",
      expectedStatus: "ready",
      identityUrl: "http://127.0.0.1:8000/v1/version",
      readyUrl: "http://127.0.0.1:8000/v1/health/ready",
      command: python,
      args: [
        "-m",
        "uvicorn",
        "app.local_main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--reload",
      ],
      cwd: apiRoot,
    },
    {
      name: "ai",
      expectedService: "ai",
      expectedStatus: "ready",
      identityUrl: "http://127.0.0.1:3101/internal/v1/version",
      readyUrl: "http://127.0.0.1:3101/internal/v1/health/ready",
      command: process.execPath,
      args: ["packages/ai/dist/src/server/index.js"],
      cwd: root,
    },
    {
      name: "dispatcher",
      command: python,
      args: ["-m", "app.workers.dispatcher"],
      cwd: apiRoot,
    },
    {
      name: "worker",
      command: python,
      args: [
        "-m",
        "celery",
        "-A",
        "app.workers.celery_app:celery_app",
        "worker",
        "-Q",
        "ai.interactive,ai.batch,file.parse,file.export,privacy",
        "--pool=solo",
        "--loglevel=INFO",
      ],
      cwd: apiRoot,
    },
  ];
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  if (existsSync(".env")) {
    process.loadEnvFile(".env");
  }
  const code = await runDevSupervisor(localDevCommands());
  process.exitCode = code;
}
