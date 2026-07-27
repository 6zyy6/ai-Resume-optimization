import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

const command = process.argv[2];
const python = resolve(".venv/bin/python");

if (!existsSync(python)) {
  throw new Error(
    "Python environment is missing. Run: python3.12 -m venv .venv && .venv/bin/python -m pip install -r packages/api/requirements.lock",
  );
}

const args = ["lint", "test", "build", "dev"].includes(command)
  ? ["scripts/python_task.py", command]
  : [command];
const result = spawnSync(python, args, { stdio: "inherit" });
process.exit(result.status ?? 1);
