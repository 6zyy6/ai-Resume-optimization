import http from "k6/http";
import { check } from "k6";
import { Rate, Trend } from "k6/metrics";

const lostWrites = new Rate("lost_writes");
const requestLatency = new Trend("non_ai_latency", true);

export const options = {
  scenarios: {
    steady: {
      executor: "constant-arrival-rate",
      rate: 50,
      timeUnit: "1s",
      preAllocatedVUs: 100,
      maxVUs: 150,
      stages: undefined,
      duration: "17m",
    },
  },
  thresholds: {
    http_req_duration: ["p(95)<500", "p(99)<1000"],
    http_req_failed: ["rate<0.01"],
    lost_writes: ["rate==0"],
  },
};

const base = __ENV.API_BASE_URL;
const cookie = __ENV.SESSION_COOKIE;

export default function () {
  const read = Math.random() < 0.7;
  const idempotency = `k6-${__VU}-${__ITER}`;
  const response = read
    ? http.get(`${base}/v1/resumes?limit=20`, { headers: { Cookie: cookie } })
    : http.post(`${base}/v1/facts`, JSON.stringify({
      kind: "performance_fixture",
      status: "unconfirmed",
      value: `fixture-${__VU}-${__ITER}`,
    }), { headers: { Cookie: cookie, "Content-Type": "application/json", "Idempotency-Key": idempotency } });
  requestLatency.add(response.timings.duration);
  const ok = check(response, { "response accepted": (item) => item.status >= 200 && item.status < 500 });
  if (!read) lostWrites.add(!ok);
}
