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
        
        # Define the desired column order
        column_order = [
            'brand',
            'part_number',
            'name',
            'price',
            'dimensions',
            'weight',
            'description',
            'compatibility'
        ]
        
        # Flatten nested dictionaries
        if 'specifications' in df.columns:
            specs_df = pd.json_normalize(df['specifications'])
            df = pd.concat([df.drop('specifications', axis=1), specs_df], axis=1)
        
        # Convert compatibility lists to strings
        if 'compatibility' in df.columns:
            df['compatibility'] = df['compatibility'].apply(lambda x: '; '.join(x) if isinstance(x, list) else x)
        
        # Reorder columns if they exist
        existing_columns = [col for col in column_order if col in df.columns]
        remaining_columns = [col for col in df.columns if col not in existing_columns]
        df = df[existing_columns + remaining_columns]
        
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