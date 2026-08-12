-- Сырой слой: то, что приезжает из источника, как оно есть.
--
-- Здесь намеренно НЕТ первичных ключей и ограничений уникальности. Сырой слой
-- повторяет выгрузку источника вместе с ее дефектами, а среди заложенных
-- дефектов есть дубли строк. Ограничение уникальности означало бы, что дубль
-- невозможно даже загрузить, - и тогда дедупликация в staging защищала бы от
-- того, чего не бывает. Проверка на уникальность зерна появится на этапе 3,
-- но применяться будет к витрине, а не к сырью.
--
-- Индексы стоят только на ключах партиций: загрузка работает через
-- delete-and-insert за дату, и без индекса каждый прогон делал бы seq scan.

CREATE SCHEMA IF NOT EXISTS raw;

-- Справочник магазинов. Единственная таблица без партиций: перегружается
-- целиком, она маленькая и меняется редко.
CREATE TABLE IF NOT EXISTS raw.stores (
    store_id      integer      NOT NULL,
    store_code    text         NOT NULL,
    city          text         NOT NULL,
    store_format  text         NOT NULL,   -- малый | средний | большой
    opened_on     date         NOT NULL
);

CREATE TABLE IF NOT EXISTS raw.orders (
    order_id      bigint       NOT NULL,
    store_id      integer      NOT NULL,
    order_ts      timestamp    NOT NULL,
    order_date    date         NOT NULL,   -- ключ партиции
    channel       text         NOT NULL,   -- офлайн | сайт | приложение
    status        text         NOT NULL,   -- оформлен | отменен
    customer_id   bigint
);
CREATE INDEX IF NOT EXISTS orders_order_date_idx ON raw.orders (order_date);

-- Позиции заказа. order_date денормализован намеренно: без него удаление
-- партиции пришлось бы делать джойном к заказам, а это ровно тот случай,
-- когда денормализация в сыром слое стоит дешевле, чем красота модели.
--
-- Про sku: это непрозрачный код позиции и ничего больше. Ни категории, ни
-- бренда, ни закупочной цены здесь нет и не будет - это категорийный
-- менеджмент, другая профессия, и граница домена держится с первого этапа.
CREATE TABLE IF NOT EXISTS raw.order_items (
    order_id      bigint       NOT NULL,
    order_date    date         NOT NULL,   -- ключ партиции
    line_no       integer      NOT NULL,
    sku           text         NOT NULL,
    quantity      integer      NOT NULL,
    unit_price    numeric(10,2) NOT NULL,
    line_amount   numeric(12,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS order_items_order_date_idx ON raw.order_items (order_date);

-- Возвраты. Ключ партиции - дата возврата, а не дата заказа: возврат приезжает
-- своим днем и меняет выручку прошлого. В этом весь смысл окна пересчета на
-- этапе 4, и структура таблицы должна это допускать с самого начала.
CREATE TABLE IF NOT EXISTS raw.returns (
    return_id       bigint       NOT NULL,
    order_id        bigint       NOT NULL,
    order_date      date         NOT NULL, -- дата исходного заказа
    line_no         integer      NOT NULL,
    returned_date   date         NOT NULL, -- ключ партиции
    quantity        integer      NOT NULL,
    returned_amount numeric(12,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS returns_returned_date_idx ON raw.returns (returned_date);

CREATE TABLE IF NOT EXISTS raw.cancellations (
    cancellation_id bigint      NOT NULL,
    order_id        bigint      NOT NULL,
    order_date      date        NOT NULL,
    cancelled_date  date        NOT NULL,  -- ключ партиции
    reason          text        NOT NULL
);
CREATE INDEX IF NOT EXISTS cancellations_cancelled_date_idx ON raw.cancellations (cancelled_date);

CREATE TABLE IF NOT EXISTS raw.store_traffic (
    store_id      integer      NOT NULL,
    traffic_date  date         NOT NULL,   -- ключ партиции
    visitors      integer      NOT NULL
);
CREATE INDEX IF NOT EXISTS store_traffic_traffic_date_idx ON raw.store_traffic (traffic_date);
