import argparse
import datetime
import glob
import json
import logging
import os
import sqlite3

import duckdb
import numpy as np
import pandas as pd
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, (np.integer, np.int32, np.int64)):
            return int(obj)
        elif isinstance(obj, (np.float32, np.float64)):
            return float(obj)
        elif isinstance(obj, np.bool_):
            return bool(obj)
        elif isinstance(obj, (datetime.datetime, datetime.date)):
            return obj.isoformat()
        return super(NumpyEncoder, self).default(obj)


def create_nested_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    new_df = pd.DataFrame()
    new_df["extracted_product"] = df.apply(lambda x: x.to_dict(), axis=1)
    new_df["catalog"] = df["catalog"]
    new_df["product_group_id"] = df["productGroupID"]
    new_df["extracted_product"] = new_df["extracted_product"].apply(
        lambda x: json.dumps(x, cls=NumpyEncoder)
    )
    return new_df


def load_to_sqlite(
    conn: sqlite3.Connection,
    parquet_files: list[str],
    table_name: str,
    is_flattened: bool = False,
) -> tuple[int, int]:
    """Load parquet files into SQLite database."""
    total_products = 0
    duplicates_skipped = 0

    if parquet_files:
        # Create table with first file
        first_df = pd.read_parquet(parquet_files[0])
        if is_flattened:
            first_df = create_nested_dataframe(first_df)
        first_df.to_sql(table_name, conn, if_exists="replace", index=False)
        
        cursor = conn.cursor()
        initial_count = cursor.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        total_products += initial_count
        logger.info(
            f"Created table {table_name} with schema from first file ({initial_count} products)"
        )

        # Create a temporary table for the product group IDs
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name}_group_ids AS
            SELECT DISTINCT json_extract(extracted_product, '$.productGroupID') as product_group_id
            FROM {table_name}
            WHERE json_extract(extracted_product, '$.productGroupID') IS NOT NULL
        """)
        conn.commit()

        # Append remaining files
        for parquet_file in parquet_files[1:]:
            logger.info(f"Loading {parquet_file} into table {table_name}")
            try:
                df = pd.read_parquet(parquet_file)
                if is_flattened:
                    df = create_nested_dataframe(df)
                
                # Count total records in the new file
                file_total = len(df)
                
                # Use temporary table for deduplication
                temp_table = f"{table_name}_temp"
                df.to_sql(temp_table, conn, if_exists="replace", index=False)
                
                # Get count before insertion
                pre_count = cursor.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                
                # Insert non-duplicate records
                cursor.execute(f"""
                    INSERT INTO {table_name}
                    SELECT t.*
                    FROM {temp_table} t
                    WHERE NOT EXISTS (
                        SELECT 1 
                        FROM {table_name}_group_ids g
                        WHERE g.product_group_id = json_extract(t.extracted_product, '$.productGroupID')
                    )
                """)
                
                # Get count after insertion
                post_count = cursor.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                inserted = post_count - pre_count
                duplicates_skipped += file_total - inserted
                
                # Update group IDs table
                cursor.execute(f"""
                    INSERT INTO {table_name}_group_ids
                    SELECT DISTINCT json_extract(extracted_product, '$.productGroupID') as product_group_id
                    FROM {temp_table}
                    WHERE json_extract(extracted_product, '$.productGroupID') IS NOT NULL
                    AND product_group_id NOT IN (SELECT product_group_id FROM {table_name}_group_ids)
                """)
                
                # Drop temporary table
                cursor.execute(f"DROP TABLE IF EXISTS {temp_table}")
                conn.commit()
                
                total_products += inserted
                logger.info(f"Successfully loaded {inserted} rows into {table_name} ({file_total - inserted} duplicates skipped)")
            except sqlite3.IntegrityError as e:
                duplicates_skipped += 1
                logger.warning(f"Skipped duplicate product group in {parquet_file}: {str(e)}")
            except Exception as e:
                logger.error(f"Error loading {parquet_file}: {str(e)}")

        # Clean up
        cursor.execute(f"DROP TABLE IF EXISTS {table_name}_group_ids")
        conn.commit()

    return total_products, duplicates_skipped


def load_to_duckdb(
    conn: duckdb.DuckDBPyConnection, parquet_files: list[str], table_name: str, is_flattened: bool = False
) -> tuple[int, int]:
    """Load parquet files into DuckDB database."""
    total_products = 0
    duplicates_skipped = 0

    if parquet_files:
        # Read first file and create table
        first_df = pd.read_parquet(parquet_files[0])
        if is_flattened:
            first_df = create_nested_dataframe(first_df)
        
        # Count total records in first file
        file_total = len(first_df)
        
        # Drop existing table if it exists
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")
        
        # Create temporary table for initial data
        conn.execute("DROP TABLE IF EXISTS temp_first")
        conn.execute("""
            CREATE TEMPORARY TABLE temp_first AS 
            SELECT *, 
                json_extract_string(extracted_product, 'productGroupID') as product_group_id 
            FROM first_df
            WHERE json_extract_string(extracted_product, 'productGroupID') IS NOT NULL
        """)
        
        # Create main table with deduplicated data
        conn.execute(f"""
            CREATE TABLE {table_name} AS 
            SELECT DISTINCT ON (product_group_id) *
            FROM temp_first
            WHERE product_group_id IS NOT NULL
            ORDER BY product_group_id, extracted_product
        """)
        
        initial_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        duplicates_skipped += file_total - initial_count
        total_products += initial_count
        
        logger.info(f"Created table {table_name} with schema from first file ({initial_count} products, {file_total - initial_count} duplicates skipped)")

        # Add unique index
        conn.execute(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_product_group_id 
            ON {table_name} (product_group_id)
        """)

        # Append remaining files
        for parquet_file in parquet_files[1:]:
            logger.info(f"Loading {parquet_file} into table {table_name}")
            try:
                df = pd.read_parquet(parquet_file)
                if is_flattened:
                    df = create_nested_dataframe(df)
                
                file_total = len(df)
                
                # Create temporary table
                conn.execute("DROP TABLE IF EXISTS temp_products")
                conn.execute("""
                    CREATE TEMPORARY TABLE temp_products AS 
                    SELECT *, 
                        json_extract_string(extracted_product, 'productGroupID') as product_group_id 
                    FROM df
                    WHERE json_extract_string(extracted_product, 'productGroupID') IS NOT NULL
                """)
                
                # Count records before insertion
                pre_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                
                # Insert non-duplicate records
                conn.execute(f"""
                    INSERT INTO {table_name}
                    SELECT t.* 
                    FROM temp_products t
                    WHERE NOT EXISTS (
                        SELECT 1 
                        FROM {table_name} m 
                        WHERE m.product_group_id = t.product_group_id
                    )
                """)
                
                # Count records after insertion
                post_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                inserted = post_count - pre_count
                duplicates_skipped += file_total - inserted
                total_products += inserted
                
                logger.info(f"Successfully loaded {inserted} rows into {table_name} ({file_total - inserted} duplicates skipped)")
            except Exception as e:
                logger.error(f"Error loading {parquet_file}: {str(e)}")

        # Clean up temporary tables
        conn.execute("DROP TABLE IF EXISTS temp_first")
        conn.execute("DROP TABLE IF EXISTS temp_products")

    return total_products, duplicates_skipped


def load_parquet_files_to_db(
    download_path: str,
    catalog: str,
    db_type: str = "sqlite",
    is_flattened: bool = False,
) -> None:
    """Load all parquet files from the download path into a SQLite or DuckDB database."""
    db_path = os.path.join(
        os.path.dirname(__file__), "..", f"{catalog}_catalog.{db_type}"
    )
    logger.info(f"Loading parquet files from {download_path} into {db_path}")

    # Connect to database based on type
    if db_type == "sqlite":
        conn = sqlite3.connect(db_path)
    else:  # duckdb
        # Explicitly disable encryption
        conn = duckdb.connect(db_path, config={"allow_unsigned_extensions": "true"})

    table_name = os.path.splitext(catalog)[0].replace(os.sep, "_")
    parquet_files = glob.glob(
        os.path.join(download_path, "**/*.parquet"), recursive=True
    )

    try:
        if db_type == "sqlite":
            total_products, duplicates_skipped = load_to_sqlite(
                conn, parquet_files, table_name, is_flattened
            )
        else:  # duckdb
            total_products, duplicates_skipped = load_to_duckdb(
                conn, parquet_files, table_name, is_flattened
            )

        logger.info(f"Finished loading all parquet files to {db_type} database.")
        logger.info(f"Total products loaded: {total_products}")
        logger.info(f"Duplicate products skipped: {duplicates_skipped}")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load Octogen catalog data to SQLite or DuckDB"
    )
    parser.add_argument(
        "--catalog",
        type=str,
        help="Name of the catalog to load",
        required=True,
    )
    parser.add_argument(
        "--download",
        type=str,
        help="Path to the downloaded parquet files",
        required=True,
    )
    parser.add_argument(
        "--db-type",
        type=str,
        choices=["sqlite", "duckdb"],
        default="sqlite",
        help="Database type to use (sqlite or duckdb)",
    )
    parser.add_argument(
        "--is-flattened",
        action="store_true",
        help="Whether the catalog is flattened",
    )

    args = parser.parse_args()

    if not os.path.exists(args.download):
        logger.error(f"Download path {args.download} does not exist")
        return
    if not load_dotenv():
        logger.error("Failed to load .env file")
        logger.error(
            "Please see README.md for more information on how to set up the .env file."
        )
        return

    load_parquet_files_to_db(
        args.download, args.catalog, args.db_type, args.is_flattened
    )


if __name__ == "__main__":
    main()
