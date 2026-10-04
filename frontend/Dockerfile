# Two stages: build the static bundle, then serve it with Nginx.
# No Node runtime reaches production — this is what keeps the frontend's
# footprint at ~0 on the 4 GB minimum target.

FROM node:22-alpine AS build
WORKDIR /build

COPY package.json ./
RUN npm install

COPY . .
RUN npm run build

FROM nginx:1.27-alpine
# nginx.conf is mounted by docker-compose so it can change without a
# rebuild; the built assets are baked in.
COPY --from=build /build/dist /usr/share/nginx/html
EXPOSE 80
