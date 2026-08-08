FROM node:22-alpine

ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=development \
    TZ=UTC

WORKDIR /srv

RUN apk add --no-cache libc6-compat

# Copy manifests first so `npm install` is cached independently of source edits.
# The lockfile glob tolerates a repo that has not generated one yet.
COPY package.json package-lock.json* ./
RUN npm install

# Source is bind-mounted over /srv in dev; this COPY keeps the image runnable alone.
COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev"]
