import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { runDevSupervisor } from "../dev-supervisor.mjs";

test("returns a child failure and terminates its sibling", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dev-supervisor-"));
  const marker = join(directory, "terminated");

  try {
    const exitCode = await runDevSupervisor([
      {
        command: process.execPath,
        args: [
          "-e",
          "process.on('SIGTERM', () => { require('node:fs').writeFileSync(process.argv[1], 'terminated'); process.exit(0); }); setInterval(() => {}, 1000);",
          marker,
        ],
      },
      {
        command: process.execPath,
        args: ["-e", "setTimeout(() => process.exit(7), 100)"],
      },
    ]);

    assert.equal(exitCode, 7);
    assert.equal(await readFile(marker, "utf8"), "terminated");
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});
