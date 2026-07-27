import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";

function exitCode(code, signal) {
  return code ?? (signal === "SIGINT" ? 130 : signal === "SIGTERM" ? 143 : 1);
}

function waitForClose(child) {
  return new Promise((resolve) => child.once("close", resolve));
}

export function runDevSupervisor(commands) {
  return new Promise((resolve) => {
    const children = commands.map(({ command, args }) => spawn(command, args, { stdio: "inherit" }));
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

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  const code = await runDevSupervisor([
    { command: "pnpm", args: ["-r", "--parallel", "dev"] },
    { command: process.execPath, args: ["scripts/run-python.mjs", "dev"] },
  ]);
  process.exitCode = code;
}
