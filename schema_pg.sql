-- Campus Parking Space Locator - PostgreSQL schema (same model as schema.sql)
-- Differences from the SQLite file: SERIAL keys, TIMESTAMP, SMALLINT 0/1 flag for is_occupied.
CREATE TABLE IF NOT EXISTS driver (
  driver_id SERIAL PRIMARY KEY,
  name      VARCHAR(100) NOT NULL,
  role      VARCHAR(50)  NOT NULL CHECK (role IN ('Faculty','Student'))
);
CREATE TABLE IF NOT EXISTS vehicle (
  plate_number VARCHAR(15) PRIMARY KEY,
  vehicle_type VARCHAR(50) NOT NULL,
  driver_id    INTEGER NOT NULL REFERENCES driver(driver_id)
);
CREATE TABLE IF NOT EXISTS parking_zone (
  zone_id        SERIAL PRIMARY KEY,
  zone_name      VARCHAR(50) NOT NULL UNIQUE,
  total_capacity INTEGER NOT NULL CHECK (total_capacity > 0)
);
CREATE TABLE IF NOT EXISTS parking_slot (
  slot_id     SERIAL PRIMARY KEY,
  zone_id     INTEGER NOT NULL REFERENCES parking_zone(zone_id),
  is_occupied SMALLINT NOT NULL DEFAULT 0 CHECK (is_occupied IN (0,1)),
  slot_label  VARCHAR(10) NOT NULL
);
CREATE TABLE IF NOT EXISTS reservation (
  res_id           SERIAL PRIMARY KEY,
  slot_id          INTEGER NOT NULL REFERENCES parking_slot(slot_id),
  plate_number     VARCHAR(15) NOT NULL REFERENCES vehicle(plate_number),
  reservation_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status           VARCHAR(15) NOT NULL CHECK (status IN ('Active','Completed','Expired','Cancelled'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_per_slot ON reservation(slot_id) WHERE status = 'Active';

-- EXT: application accounts (authentication, roles and ownership)
CREATE TABLE IF NOT EXISTS app_user (
  user_id       SERIAL PRIMARY KEY,
  username      VARCHAR(40)  NOT NULL UNIQUE,
  email         VARCHAR(120) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  role          VARCHAR(10)  NOT NULL CHECK (role IN ('Driver','Admin')),
  driver_id     INTEGER UNIQUE REFERENCES driver(driver_id),
  is_active     SMALLINT NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_app_user_role ON app_user(role);
