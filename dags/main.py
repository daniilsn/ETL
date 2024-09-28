from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from airflow.operators.dummy_operator import DummyOperator
from airflow.operators.bash import BashOperator
from airflow.utils.dates import days_ago
from clickhouse_driver import Client
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, to_date, regexp_replace, year, expr
from pyspark.sql.types import IntegerType
import subprocess
import requests
import re


PATH = '/opt/airflow/row_data/russian_houses.csv'
client = Client(host='clickhouse', port=9000,
                user='admin', password='admin')


default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': days_ago(1),
}

dag = DAG(
    'main',
    default_args=default_args,
    description='A simple DAG to interact with ClickHouse, PostgreSQL, and PySpark',
    schedule_interval=None,
)

def query_clickhouse(**kwargs):
    response = requests.get('http://clickhouse_user:8123/?query=SELECT%20version()')
    if response.status_code == 200:
        print(f"ClickHouse version: {response.text}")
    else:
        print(f"Failed to connect to ClickHouse, status code: {response.status_code}")

def query_postgres(**kwargs):
    command = [
        'psql',
        '-h', 'postgres_user',
        '-U', 'user',
        '-d', 'test',
        '-c', 'SELECT version();'
    ]
    env = {"PGPASSWORD": "password"}
    result = subprocess.run(command, env=env, capture_output=True, text=True)

    if result.returncode == 0:
        print(f"PostgreSQL version: {result.stdout}")
    else:
        print(f"Failed to connect to PostgreSQL, error: {result.stderr}")

def spark_session():
    spark = SparkSession.builder \
        .appName("Airflow Spark Session") \
        .getOrCreate()
    spark.conf.set("spark.sql.legacy.timeParserPolicy", "CORRECTED")

    return spark

def etl():
    
    spark = spark_session()
    spark.conf.set("spark.sql.legacy.timeParserPolicy", "CORRECTED")

    df = spark.read.option("header", "true") \
                .option("quote", '"') \
                .option("escape", '"') \
                .option("multiLine", "true") \
                .option("encoding", "utf-16") \
                .csv(PATH)


    df = df.withColumn('house_id', col('house_id').cast('integer'))
    df = df.withColumn('latitude', col('latitude').cast('float'))
    df = df.withColumn('longitude', col('longitude').cast('float'))
    df = df.withColumn('square', regexp_replace(col('square'), " ", ""))
    df = df.withColumn('square', col('square').cast('float'))
    df = df.withColumn('population', col('population').cast('integer'))
    df = df.withColumn('maintenance_year', year(col('maintenance_year')))
    df = df.withColumn('communal_service_id', col('communal_service_id').cast('integer'))
    df_clear = df.dropna()

    #IQR method for maintenance_year
    q1 = df_clear.select(expr("percentile_approx(maintenance_year, 0.25)")).collect()[0][0]
    q3 = df_clear.select(expr("percentile_approx(maintenance_year, 0.75)")).collect()[0][0]
    iqr = q3 - q1
    df_clear = df_clear.filter((col("maintenance_year") >= (q1 - 1.5 * iqr)) & (col("maintenance_year") <= (q3 + 1.5 * iqr)))


    df_clear.createOrReplaceTempView("df_clear")

    avg_year = spark.sql("""
    SELECT CAST(avg_year as INT) as avg_year
    FROM
    (SELECT avg(maintenance_year) as avg_year
    FROM df_clear) t
    """)

    median_year = spark.sql("""
    SELECT percentile_approx(maintenance_year, 0.5) as median_year
    FROM df_clear
    """)

    top_region_object = spark.sql("""
    SELECT region, count(region) as count
    FROM df_clear
    GROUP BY region
    ORDER BY count DESC
    LIMIT 10
    """)

    top_city_object = spark.sql("""
    SELECT locality_name, count(locality_name) as count
    FROM df_clear
    GROUP BY locality_name
    ORDER BY count DESC
    LIMIT 10
    """)

    min_square_per_region = spark.sql("""
    SELECT house_id, region, min
    FROM
    (SELECT house_id, region, min(square) OVER (PARTITION BY region) as min
    FROM df_clear
    ) t
    ORDER BY min
    """)

    max_square_per_region = spark.sql("""
    SELECT house_id, region, max
    FROM
    (SELECT house_id, region, max(square) OVER (PARTITION BY region) as max
    FROM df_clear
    ) t
    ORDER BY max
    """)

    #Find min and max maintenance_year
    mm = spark.sql("""
    SELECT min(maintenance_year), max(maintenance_year)
    FROM df_clear
    """)

    count_per_year = spark.sql("""
    SELECT age, count(age) as count
    FROM
    (SELECT house_id, maintenance_year, 
    CASE
    WHEN maintenance_year BETWEEN 1920 and 1929 THEN 1920
    WHEN maintenance_year BETWEEN 1930 and 1939 THEN 1930
    WHEN maintenance_year BETWEEN 1940 and 1949 THEN 1940
    WHEN maintenance_year BETWEEN 1950 and 1959 THEN 1950
    WHEN maintenance_year BETWEEN 1960 and 1969 THEN 1960
    WHEN maintenance_year BETWEEN 1970 and 1979 THEN 1970
    WHEN maintenance_year BETWEEN 1980 and 1989 THEN 1980
    WHEN maintenance_year BETWEEN 1990 and 1999 THEN 1990
    WHEN maintenance_year BETWEEN 2000 and 2009 THEN 2000
    WHEN maintenance_year BETWEEN 2010 and 2019 THEN 2010
    END as age
    FROM df_clear) t
    GROUP BY age
    ORDER BY age
    """)
    avg_year.show()
    median_year.show()
    top_region_object.show()
    top_city_object.show()
    min_square_per_region.show()
    max_square_per_region.show()
    count_per_year.show()
    
    client.execute("""
        CREATE TABLE IF NOT EXISTS default.russian_houses (
            house_id Int32,
            latitude Float32,
            longitude Float32,
            maintenance_year Int16,
            square Float32,
            population Int32,
            region String,
            locality_name String,
            address String,
            full_address String, 
            communal_service_id Int32,
            description String
            )
        ENGINE = MergeTree()
        ORDER BY house_id;
         """)
      
    data = df_clear.collect()
    data_tuples = [(row['house_id'], row['latitude'], row['longitude'], row['maintenance_year'], row['square'], 
                    row['population'], row['region'], row['locality_name'], row['address'], row['full_address'],
                    row['communal_service_id'], row['description']) for row in data]   
    client.execute('INSERT INTO default.russian_houses VALUES', data_tuples)

    spark.stop()

def top_25_houses():
    sql = """
    SELECT house_id, square 
    FROM russian_houses
    WHERE square > 60
    ORDER BY square
    LIMIT 25
    """
    result = client.execute(sql)
    for row in result:
        print(row)
    
    
download_file = BashOperator(
    task_id='download_file',
    bash_command='curl -L "https://www.dropbox.com/scl/fi/xybeqctb7gnow5spp03ln/russian_houses.csv?rlkey=9gafw4h5wlrmywt5ogrqmhfxy&st=j1r65rqy&dl=1" -o /opt/airflow/row_data/russian_houses.csv',
    dag=dag,
)

task_query_clickhouse = PythonOperator(
    task_id='query_clickhouse',
    python_callable=query_clickhouse,
    dag=dag,
)

task_query_postgres = PythonOperator(
    task_id='query_postgres',
    python_callable=query_postgres,
    dag=dag,
)

task_etl= PythonOperator(
    task_id='etl_process',
    python_callable=etl,
    provide_context=True,
    dag=dag,
)

task_show_house = PythonOperator(
    task_id='show_top_houses',
    python_callable=top_25_houses,
    dag=dag,
)


task_query_clickhouse >> task_query_postgres  >> download_file >> task_etl >> task_show_house
