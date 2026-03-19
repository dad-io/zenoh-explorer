FROM rust:1.93-alpine

EXPOSE 7447

RUN apk add --no-cache musl-dev

COPY Cargo.toml .
COPY src ./src

RUN cargo build --release

CMD ["cargo", "run", "--release"]
