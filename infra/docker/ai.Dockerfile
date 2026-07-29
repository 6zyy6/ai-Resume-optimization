FROM node:22-bookworm-slim AS build
WORKDIR /repo
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY packages/ai/package.json packages/ai/package.json
RUN pnpm install --frozen-lockfile --filter @resume/ai...
COPY packages/ai packages/ai
RUN pnpm --filter @resume/ai build

FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production
WORKDIR /repo
COPY --from=build /repo/node_modules ./node_modules
COPY --from=build /repo/packages/ai ./packages/ai
EXPOSE 3101
CMD ["node", "packages/ai/dist/server/index.js"]
