# pylint: disable=unused-argument
# pylint: disable=redefined-outer-name
# pylint: disable=line-too-long

import pytest

from helpers.client import QueryRuntimeException
from helpers.cluster import ClickHouseCluster

cluster = ClickHouseCluster(__file__)

def make_instance(name, cfg):
    return cluster.add_instance(name,
        with_zookeeper=True,
        main_configs=['configs/remote_servers.xml', cfg],
        user_configs=['configs/users.xml'])
# _n1/_n2 contains cluster with different <secret> -- should fail
n1 = make_instance('n1', 'configs/remote_servers_n1.xml')
n2 = make_instance('n2', 'configs/remote_servers_n2.xml')

users = pytest.mark.parametrize('user,password', [
    ('default', ''   ),
    ('nopass',  ''   ),
    ('pass',    'foo'),
])

def bootstrap():
    for n in list(cluster.instances.values()):
        n.query('DROP TABLE IF EXISTS data')
        n.query('DROP TABLE IF EXISTS dist')
        n.query('CREATE TABLE data (key Int) Engine=Memory()')
        n.query("""
        CREATE TABLE dist_insecure AS data
        Engine=Distributed(insecure, currentDatabase(), data, key)
        """)
        n.query("""
        CREATE TABLE dist_secure AS data
        Engine=Distributed(secure, currentDatabase(), data, key)
        """)
        n.query("""
        CREATE TABLE dist_secure_disagree AS data
        Engine=Distributed(secure_disagree, currentDatabase(), data, key)
        """)
        n.query("""
        CREATE TABLE dist_secure_buffer AS dist_secure
        Engine=Buffer(currentDatabase(), dist_secure,
            /* settings for manual flush only */
            1,    /* num_layers */
            10e6, /* min_time, placeholder */
            10e6, /* max_time, placeholder */
            0,    /* min_rows   */
            10e6, /* max_rows   */
            0,    /* min_bytes  */
            80e6  /* max_bytes  */
        )
        """)

@pytest.fixture(scope='module', autouse=True)
def start_cluster():
    try:
        cluster.start()
        bootstrap()
        yield cluster
    finally:
        cluster.shutdown()

def query_with_id(node, id_, query, **kwargs):
    return node.query("WITH '{}' AS __id {}".format(id_, query), **kwargs)

# @return -- [user, initial_user]
def get_query_user_info(node, query_pattern):
    node.query("SYSTEM FLUSH LOGS")
    return node.query("""
    SELECT user, initial_user
    FROM system.query_log
    WHERE
        query LIKE '%{}%' AND
        query NOT LIKE '%system.query_log%' AND
        type = 'QueryFinish'
    """.format(query_pattern)).strip().split('\t')

def test_insecure():
    n1.query('SELECT * FROM dist_insecure')

def test_insecure_insert_async():
    n1.query('INSERT INTO dist_insecure SELECT * FROM numbers(2)')
    n1.query('SYSTEM FLUSH DISTRIBUTED ON CLUSTER insecure dist_insecure')
    assert int(n1.query('SELECT count() FROM dist_insecure')) == 2
    n1.query('TRUNCATE TABLE data ON CLUSTER insecure')

def test_insecure_insert_sync():
    n1.query('INSERT INTO dist_insecure SELECT * FROM numbers(2)', settings={'insert_distributed_sync': 1})
    assert int(n1.query('SELECT count() FROM dist_insecure')) == 2
    n1.query('TRUNCATE TABLE data ON CLUSTER secure')

def test_secure():
    n1.query('SELECT * FROM dist_secure')

def test_secure_insert_async():
    n1.query('INSERT INTO dist_secure SELECT * FROM numbers(2)')
    n1.query('SYSTEM FLUSH DISTRIBUTED ON CLUSTER secure dist_secure')
    assert int(n1.query('SELECT count() FROM dist_secure')) == 2
    n1.query('TRUNCATE TABLE data ON CLUSTER secure')

def test_secure_insert_sync():
    n1.query('INSERT INTO dist_secure SELECT * FROM numbers(2)', settings={'insert_distributed_sync': 1})
    assert int(n1.query('SELECT count() FROM dist_secure')) == 2
    n1.query('TRUNCATE TABLE data ON CLUSTER secure')

# INSERT w/o initial_user
#
# Buffer() flush happens with global context, that does not have user
# And so Context::user/ClientInfo::current_user/ClientInfo::initial_user will be empty
def test_secure_insert_buffer_async():
    n1.query('INSERT INTO dist_secure_buffer SELECT * FROM numbers(2)')
    n1.query('SYSTEM FLUSH DISTRIBUTED ON CLUSTER secure dist_secure')
    # no Buffer flush happened
    assert int(n1.query('SELECT count() FROM dist_secure')) == 0
    n1.query('OPTIMIZE TABLE dist_secure_buffer')
    # manual flush
    n1.query('SYSTEM FLUSH DISTRIBUTED ON CLUSTER secure dist_secure')
    assert int(n1.query('SELECT count() FROM dist_secure')) == 2
    n1.query('TRUNCATE TABLE data ON CLUSTER secure')

def test_secure_disagree():
    with pytest.raises(QueryRuntimeException, match='.*Hash mismatch.*'):
        n1.query('SELECT * FROM dist_secure_disagree')

def test_secure_disagree_insert():
    n1.query('INSERT INTO dist_secure_disagree SELECT * FROM numbers(2)')
    with pytest.raises(QueryRuntimeException, match='.*Hash mismatch.*'):
        n1.query('SYSTEM FLUSH DISTRIBUTED ON CLUSTER secure_disagree dist_secure_disagree')
    # check the the connection will be re-established
    # IOW that we will not get "Unknown BlockInfo field"
    with pytest.raises(QueryRuntimeException, match='.*Hash mismatch.*'):
        assert int(n1.query('SELECT count() FROM dist_secure_disagree')) == 0

@users
def test_user_insecure_cluster(user, password):
    id_ = 'query-dist_insecure-' + user
    query_with_id(n1, id_, 'SELECT * FROM dist_insecure', user=user, password=password)
    assert get_query_user_info(n1, id_) == [user, user] # due to prefer_localhost_replica
    assert get_query_user_info(n2, id_) == ['default', user]

@users
def test_user_secure_cluster(user, password):
    id_ = 'query-dist_secure-' + user
    query_with_id(n1, id_, 'SELECT * FROM dist_secure', user=user, password=password)
    assert get_query_user_info(n1, id_) == [user, user]
    assert get_query_user_info(n2, id_) == [user, user]

# settings in the protocol cannot be used since they will be applied to early
# and it will not even enter execution of distributed query
@users
def test_per_user_settings_insecure_cluster(user, password):
    id_ = 'query-settings-dist_insecure-' + user
    query_with_id(n1, id_, """
    SELECT * FROM dist_insecure
    SETTINGS
        prefer_localhost_replica=0,
        max_memory_usage_for_user=100,
        max_untracked_memory=0
    """, user=user, password=password)
@users
def test_per_user_settings_secure_cluster(user, password):
    id_ = 'query-settings-dist_secure-' + user
    with pytest.raises(QueryRuntimeException):
        query_with_id(n1, id_, """
        SELECT * FROM dist_secure
        SETTINGS
            prefer_localhost_replica=0,
            max_memory_usage_for_user=100,
            max_untracked_memory=0
        """, user=user, password=password)

# TODO: check user for INSERT
