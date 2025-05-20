import pandas as pd
from typing import List, Dict
import logging
import os

logger = logging.getLogger(__name__)

def save_to_csv(data: List[Dict], filename: str) -> bool:
    """
    Save the scraped data to a CSV file.
    
    Args:
        data: List of dictionaries containing part data
        filename: Name of the CSV file to save
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Create output directory if it doesn't exist
        os.makedirs('output', exist_ok=True)
        
        # Convert data to DataFrame
        df = pd.DataFrame(data)
        
        # Save to CSV
        filepath = os.path.join('output', filename)

        # Check if file exists to avoid writing header again
        write_header = not os.path.isfile(filepath)

        df.to_csv(filepath, mode='a', index=False, header=write_header)
        logger.info(f"Data appended to {filepath}")
        return True
        
    except Exception as e:
        logger.error(f"Error saving data to CSV: {str(e)}")
        return False 