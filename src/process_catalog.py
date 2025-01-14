import argparse
import asyncio
import glob
import logging
import os
from typing import Optional

from dotenv import load_dotenv

# Import functions from existing scripts
from download_catalog_files import download_catalog
from index_catalog import create_whoosh_index
from load_to_db import load_parquet_files_to_db
from utils import get_catalog_db_path

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def process_catalog(
    catalog: str,
    download_to: str,
    index_dir: Optional[str] = None,
    batch_size: int = 1000,
    read_from_local_files: bool = False,
) -> None:
    """Process a catalog through all three steps: download, load to DB, and index."""
    octogen_catalog_bucket = os.getenv("OCTOGEN_CATALOG_BUCKET_NAME")
    octogen_customer_name = os.getenv("OCTOGEN_CUSTOMER_NAME")
    try:
        # Step 1: Download catalog
        if read_from_local_files:
            # Ensure download_to ends with catalog={catalog}
            expected_catalog_suffix = f"catalog={catalog}"
            if not download_to.endswith(expected_catalog_suffix):
                download_to = os.path.join(download_to, expected_catalog_suffix)

            # Find the latest snapshot directory
            snapshot_pattern = os.path.join(download_to, "snapshot=*")
            snapshot_dirs = sorted(glob.glob(snapshot_pattern), reverse=True)

            if not snapshot_dirs:
                raise ValueError(f"No snapshot directories found in {download_to}")

            download_to = snapshot_dirs[0]  # Use the most recent snapshot
            logger.info(
                f"Step 1: Reading catalog {catalog} from local files in {download_to}"
            )
        else:
            logger.info(f"Step 1: Downloading catalog {catalog}")
            await download_catalog(
                octogen_catalog_bucket=octogen_catalog_bucket,
                octogen_customer_name=octogen_customer_name,
                catalog=catalog,
                download_path=download_to,
            )

        # Step 2: Load to database
        logger.info(f"Step 2: Loading catalog {catalog} to DuckDB database")
        db_path = get_catalog_db_path(catalog, raise_if_not_found=False)
        logger.debug(f"Using database path: {db_path}")
        load_parquet_files_to_db(download_to, catalog, create_if_missing=True)

        # Step 3: Index the data
        logger.info(f"Step 3: Indexing catalog {catalog}")
        if not index_dir:
            index_dir = f"/tmp/whoosh/{catalog}"

        create_whoosh_index(db_path, index_dir, catalog, batch_size)

        logger.info(f"Successfully processed catalog {catalog}")

    except Exception as e:
        logger.error(f"Error processing catalog {catalog}: {e}")
        raise


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process Octogen catalog: download, load to DB, and index"
    )
    parser.add_argument(
        "--catalog", type=str, help="Name of the catalog to process", required=True
    )
    parser.add_argument(
        "--download",
        type=str,
        required=False,
        default="octogen-catalog-exchange",
        help="Path where catalog files will be downloaded",
    )
    parser.add_argument(
        "--index_dir",
        type=str,
        help="Directory to store the Whoosh index (default: /tmp/whoosh/<catalog>)",
    )
    parser.add_argument(
        "--batch_size", type=int, default=1000, help="Batch size for indexing"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        default=False,
        help="Read catalog from local files instead of downloading from GCS",
    )

    args = parser.parse_args()
    if not load_dotenv():
        logger.error("Failed to load .env file")
        logger.error(
            "Please see README.md for more information on how to set up the .env file."
        )
        return

    download_to: str = args.download
    if args.local:
        download_to = os.path.join(download_to, f"catalog={args.catalog}")
    else:
        octogen_customer_name = os.getenv("OCTOGEN_CUSTOMER_NAME")

        if octogen_customer_name not in download_to:
            download_to = os.path.join(
                download_to, octogen_customer_name, f"catalog={args.catalog}"
            )

    await process_catalog(
        catalog=args.catalog,
        download_to=download_to,
        index_dir=args.index_dir,
        batch_size=args.batch_size,
        read_from_local_files=args.local,
    )


if __name__ == "__main__":
    asyncio.run(main())
