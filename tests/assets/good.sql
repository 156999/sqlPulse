-- weight: 100
SELECT * FROM orders WHERE id = FLOOR(1 + RAND()*10000);
