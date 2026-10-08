// Test fixture for the MongoDB backup source. Runs once, as root.

const shop = db.getSiblingDB("shop");
shop.orders.insertMany(Array.from({ length: 5000 }, (_, i) => ({ n: i, item: "item" + (i % 50), qty: i % 7 })));
shop.orders.createIndex({ item: 1 });
shop.products.insertMany(Array.from({ length: 50 }, (_, i) => ({ _id: "item" + i, price: i * 1.25 })));
shop.createView("big_orders", "orders", [{ $match: { qty: { $gt: 5 } } }]);
shop.createUser({ user: "shop_app", pwd: "shop-pw", roles: [{ role: "readWrite", db: "shop" }] });

const logs = db.getSiblingDB("logs");
logs.events.insertMany(Array.from({ length: 2000 }, (_, i) => ({ ts: new Date(1700000000000 + i * 1000), level: i % 3 })));

db.getSiblingDB("scratch").junk.insertOne({ a: 1 });

// Least-privilege backup user.
db.getSiblingDB("admin").createUser({ user: "dbbackup", pwd: "dbbackup-pw", roles: ["backup"] });
