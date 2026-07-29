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

export function runDevSupervisor(commands) {
  return new Promise((resolve) => {
    const children = commands.map(({ command, args, cwd }) =>
      spawn(command, args, { cwd, stdio: "inherit" }),
    );
    let finished = false;

    const removeSignalHandlers = () => {
      process.off("SIGINT", onSigint);
      process.off("SIGTERM", onSigterm);
    };

    const finish = (origin, code, signal) => {
      if (finished) return;
      finished = true;
      removeSignalHandlers();
      const siblings = children.filter((child) => child !== origin && child.exitCode === null);
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
    for (const child of children) {
      child.once("error", () => finish(child, 1));
      child.once("exit", (code, signal) => finish(child, code, signal));
    }
  });
}

export function localDevCommands(root = process.cwd()) {
  const apiRoot = resolve(root, "packages/api");
  const python = resolve(root, ".venv/bin/python");
  return [
    {
      name: "web",
      command: "pnpm",
      args: ["--filter", "@resume/web", "dev"],
      cwd: root,
    },
    {
      name: "api",
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
