export function GET() {
  return Response.json({
    commit_sha: process.env.APP_COMMIT_SHA ?? "development",
    service: "web",
  });
}
