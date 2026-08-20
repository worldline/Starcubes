\pset tuples_only

-- minutes from 0 to 1339 by response
SELECT translate(array_agg(array[t.time, t.r, t.c])::text, '{}', '[]')
FROM (
SELECT trunc(extract(epoch from create_time-'2022-03-19 00:00:00')/60) as time, response as r, count(*) as c
  FROM fact
 WHERE create_time between '2022-03-19 00:00:00' AND '2022-03-20 00:00:00'
 GROUP BY trunc(extract(epoch from create_time-'2022-03-19 00:00:00')/60), response order by 1,2
) t;

-- minutes from 0 to 1339 by db_loc
SELECT translate(array_agg(array[t.time, t.r, t.c])::text, '{}', '[]')
FROM (
SELECT trunc(extract(epoch from create_time-'2022-02-19 00:00:00')/60) as time, db_loc as r, count(*) as c
  FROM fact
 WHERE create_time between '2022-02-19 00:00:00' AND '2022-02-20 00:00:00'
 GROUP BY 1, 2 order by 1,2
) t;

-- date, hour, minute bucket by response
SELECT date(create_time), trunc(extract(epoch from create_time-'2021-12-05 00:00:00')/3600) as h, mod(trunc(extract(epoch from create_time-'2021-12-05 00:00:00')/60),60) as m, response as r, count(*) as c
  FROM fact
 WHERE create_time between '2021-12-05 00:00:00' AND '2021-12-06 00:00:00'
 GROUP BY date(create_time), trunc(extract(epoch from create_time-'2021-12-05 00:00:00')/3600), mod(trunc(extract(epoch from create_time-'2021-12-05 00:00:00')/60),60), response order by 1,2

-- aggregate for all combinations of given dimensions
SELECT DISTINCT f.app_loc,f.src_peer,f.so_peer_1,f.so_peer_2,f.so_peer_3,f.cnt from (
    SELECT app_loc,src_peer,so_peer_1,so_peer_2,so_peer_3,count(*) cnt
    FROM fact
    WHERE create_time between '2022-03-19 00:00:00' AND '2022-03-20 00:00:00'
    GROUP BY
        cube(app_loc,src_peer),
        cube(app_loc,so_peer_1),
        cube(app_loc,so_peer_2),
        cube(app_loc,so_peer_3)
) as f order by cnt desc;

explain (analyze) select count(*) from (select app_loc,db_loc,response,txn_type,sec_type,txn_origin,so_peer_1,so_peer_2,so_peer_3,src_peer,merch_cat,card_type,issuer,count(*) from fact where create_time between '2022-03-21' and '2022-03-22' group by 1,2,3,4,5,6,7,8,9,10,11,12,13) as f;

set search_path TO star;
\pset tuples_only
SELECT translate(json_build_object(
    'name', 'by src_peer',
    'type', 'multibar',
    'max', max(o.c),
    'min', min(o.c),
    'sum', sum(o.c),
    'count', count(o.c),
    'interval', 60,
    'interval_start', floor(extract(epoch from 'yesterday'::timestamptz)),
    'interval_end', floor(extract(epoch from 'today'::timestamptz)),
    'dim', array_agg(DISTINCT dim order by dim),
    'data', translate(json_object_agg(o.t, o.v)::text, '" ', ''))::text, ' ', '')
FROM (
    SELECT i.t, json_object_agg(i.d, i.c) v, array_agg(i.d) d, sum(i.c) c, count(distinct i.d) dc from (
        SELECT trunc(extract(epoch from create_time-'yesterday')/60) as t, src_peer d, count(*) as c
          FROM fact
         WHERE create_time between 'yesterday' AND 'today' AND txn_origin!=4
      GROUP BY 1, 2 order by 1,2
    ) as i GROUP BY 1
) as o, unnest(o.d) dim;

SELECT translate(json_build_object(
                'name', 'src_peer',
                'type', 'multibar',
                'max', max(o.c),
                'min', min(o.c),
                'sum', sum(o.c),
                'count', count(o.c),
                'interval', 60,
                'interval_start', floor(extract(epoch from 'yesterday'::timestamptz)),
                'interval_end', floor(extract(epoch from now())),
                'dim', array_agg(DISTINCT dim order by dim),
                'data', translate(json_object_agg(o.t, o.v)::text, ' ', '')::json)::text, ' ', '')
            FROM (
                SELECT i.t, json_object_agg(i.d, i.c) v, array_agg(i.d) d, sum(i.c) c, count(distinct i.d) dc from (
                    SELECT trunc(extract(epoch from create_time-'yesterday')/60) as t, src_peer d, count(*) as c
                      FROM star.fact
                     WHERE create_time between 'yesterday' AND now() AND txn_origin!=4
                  GROUP BY 1, 2 order by 1,2
                ) as i GROUP BY 1
            ) as o, unnest(o.d) dim;

