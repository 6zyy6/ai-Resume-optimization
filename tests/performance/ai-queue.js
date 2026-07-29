import http from "k6/http";
import { check, sleep } from "k6";
import { Trend } from "k6/metrics";

const admissionLatency = new Trend("ai_admission_latency", true);
const queueLatency = new Trend("ai_queue_latency", true);

export const options = {
  scenarios: {
    ai_queue: { executor: "shared-iterations", vus: 20, iterations: 100, maxDuration: "10m" },
  },
  thresholds: {
    ai_admission_latency: ["p(95)<1000"],
    ai_queue_latency: ["p(95)<25000"],
    checks: ["rate==1"],
  },
};

export default function () {
  const started = Date.now();
  const response = http.post(`${__ENV.API_BASE_URL}/v1/match-analyses`, __ENV.MATCH_PAYLOAD, {
    headers: {
      Cookie: __ENV.SESSION_COOKIE,
      "Content-Type": "application/json",
      "Idempotency-Key": `k6-ai-${__VU}-${__ITER}`,
    },
  });
  admissionLatency.add(response.timings.duration);
  check(response, { "task admitted": (item) => item.status === 202 });
  const taskId = response.json("task_id");
  while (taskId && Date.now() - started < 120000) {
    const task = http.get(`${__ENV.API_BASE_URL}/v1/tasks/${taskId}`, { headers: { Cookie: __ENV.SESSION_COOKIE } });
    const status = task.json("status");
    if (["succeeded", "failed", "cancelled"].includes(status)) {
      queueLatency.add(Date.now() - started);
      check(task, { "task succeeded": () => status === "succeeded" });
      return;
    }
    sleep(1);
  }
  check(null, { "task reached terminal state": () => false });
}
