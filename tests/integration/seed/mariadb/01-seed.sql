-- Test fixture for the MariaDB backup source. Runs once, as root.

SET SESSION max_recursive_iterations = 1000000;

CREATE DATABASE shop;
CREATE DATABASE `wiki-db`;
CREATE DATABASE scratch;

USE shop;
CREATE TABLE products (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    price DECIMAL(10, 2),
    img BLOB
) ENGINE = InnoDB;
INSERT INTO products (name, price, img)
WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 3000)
SELECT CONCAT('product ', n), n * 0.99, UNHEX(MD5(n)) FROM seq;
CREATE TABLE orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL,
    qty INT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products (id)
) ENGINE = InnoDB;
INSERT INTO orders (product_id, qty)
WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 10000)
SELECT (n MOD 3000) + 1, n MOD 7 FROM seq;
-- Non-InnoDB table: should produce a consistency warning.
CREATE TABLE legacy_log (id INT PRIMARY KEY, msg TEXT) ENGINE = Aria;
INSERT INTO legacy_log VALUES (1, 'hello'), (2, 'world');
CREATE VIEW cheap_products AS SELECT id, name FROM products WHERE price < 10;
CREATE PROCEDURE order_count(IN p INT) SELECT COUNT(*) FROM orders WHERE product_id = p;
CREATE TRIGGER orders_qty BEFORE INSERT ON orders FOR EACH ROW SET NEW.qty = GREATEST(NEW.qty, 0);
CREATE EVENT nightly_noop ON SCHEDULE EVERY 1 DAY DISABLE DO SELECT 1;

USE `wiki-db`;
CREATE TABLE pages (id INT PRIMARY KEY, title VARCHAR(200), body MEDIUMTEXT) ENGINE = InnoDB;
INSERT INTO pages
WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 500)
SELECT n, CONCAT('Page ', n), REPEAT('lorem ipsum ', 50) FROM seq;

USE scratch;
CREATE TABLE junk (x INT);

CREATE USER 'shop_app'@'%' IDENTIFIED BY 'shop-pw';
GRANT SELECT, INSERT, UPDATE, DELETE ON shop.* TO 'shop_app'@'%';
CREATE USER 'wiki_ro'@'%' IDENTIFIED BY 'wiki-pw';
-- Table-level grant: only restorable once the table exists.
GRANT SELECT ON `wiki-db`.pages TO 'wiki_ro'@'%';
CREATE ROLE reporting;
GRANT SELECT ON shop.* TO reporting;
GRANT reporting TO 'shop_app'@'%';

-- Least-privilege backup user.
-- Awkward password: exercises the option-file quoting (see tests/integration/secrets).
CREATE USER 'dbbackup'@'%' IDENTIFIED BY 'dbb#ack"up\\pw\'x';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, PROCESS, RELOAD, SHOW DATABASES ON *.* TO 'dbbackup'@'%';
