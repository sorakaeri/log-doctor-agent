-- 최초 기동 시 1회 실행되는 초기 데이터
CREATE TABLE IF NOT EXISTS items (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(100) NOT NULL,
    price      INTEGER      NOT NULL CHECK (price >= 0),
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

INSERT INTO items (name, price) VALUES
    ('키보드', 45000),
    ('마우스', 21000),
    ('모니터', 189000);
