-- Campus Parking Space Locator - database schema (SQLite)
-- Based on the Midterm data dictionary; extensions are marked EXT.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS driver (
  driver_id INTEGER PRIMARY KEY AUTOINCREMENT,
  name      VARCHAR(100) NOT NULL,
  role      VARCHAR(50)  NOT NULL CHECK (role IN ('Faculty','Student'))
);

CREATE TABLE IF NOT EXISTS vehicle (
  plate_number VARCHAR(15) PRIMARY KEY NOT NULL,
  vehicle_type VARCHAR(50) NOT NULL,
  driver_id    INTEGER NOT NULL REFERENCES driver(driver_id)
);

CREATE TABLE IF NOT EXISTS parking_zone (
  zone_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_name      VARCHAR(50) NOT NULL UNIQUE,
  total_capacity INTEGER NOT NULL CHECK (total_capacity > 0)
);

CREATE TABLE IF NOT EXISTS parking_slot (
  slot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_id     INTEGER NOT NULL REFERENCES parking_zone(zone_id),
  is_occupied BOOLEAN NOT NULL DEFAULT 0,
  slot_label  VARCHAR(10) NOT NULL            -- EXT: display label, e.g. A-02
);

CREATE TABLE IF NOT EXISTS reservation (
  res_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  slot_id          INTEGER NOT NULL REFERENCES parking_slot(slot_id),
  plate_number     VARCHAR(15) NOT NULL REFERENCES vehicle(plate_number),
  reservation_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status           VARCHAR(15) NOT NULL
                   CHECK (status IN ('Active','Completed','Expired','Cancelled')) -- EXT: Cancelled
);
-- Business rule 3: one Active reservation per slot (enforced by the database)
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_per_slot
  ON reservation(slot_id) WHERE status = 'Active';
