from anthropic import AnthropicVertex
import logging
from functools import lru_cache
import os

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@lru_cache(maxsize=1000)
def _cached_call_llm(prompt: str) -> str:
    return "This is a test response"

def call_llm(prompt: str, use_cache: bool = True) -> str:
    """
    Call the LLM with the given prompt.
    
    Args:
        prompt (str): The prompt to send to the LLM
        use_cache (bool): Whether to use cached results. Defaults to True.
    
    Returns:
        str: The LLM's response text
    """
    logger.info(f"Calling LLM with prompt: {prompt}...")
    
    if use_cache:
        response = _cached_call_llm(prompt)
    else:
        # Call directly without cache
        response = _cached_call_llm.__wrapped__(prompt)
    
    logger.info(f"LLM response received (first 100 chars): {response}...")
    return response
            
def main():
    """Test the LLM call functionality."""
    test_prompt = "Hello, how are you?"
    try:
        response = call_llm(test_prompt)
        print(f"Test successful. Response: {response}")
    except Exception as e:
        print(f"Test failed: {str(e)}")

if __name__ == "__main__":
    main()