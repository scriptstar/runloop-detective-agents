import logging
import os
import sys
from runloop_api_client import Runloop
import openai
import ell
from ell import Message
from typing import List
from runloop_api_client.types import DevboxView
from dotenv import load_dotenv, find_dotenv

_ = load_dotenv(find_dotenv())  # read local .env file

logger = logging.getLogger("log_detective")

SYSTEM_PROMPT = """
You are an expert log analyst and system detective specializing in identifying patterns, anomalies, and actionable insights from log files.

Your capabilities include:
- Pattern recognition in timestamps, error messages, and system events
- Statistical analysis of log data (frequencies, trends, correlations)
- Anomaly detection for unusual patterns or outliers
- Root cause analysis suggestions
- Performance bottleneck identification
- Cross-correlation analysis between different log events

Always provide clear, actionable insights with specific evidence from the logs.
""".strip()

USER_PROMPT_TEMPLATE = """
I need you to analyze the log file '{filename}' that I've uploaded to the devbox.

Please perform a comprehensive analysis including:

1. **Overview**: File size, date range, total number of entries
2. **Event Types**: What types of events/messages are logged
3. **Error Analysis**: Any errors, warnings, or failure patterns
4. **Timeline Analysis**: Key events, busy periods, patterns over time
5. **Anomaly Detection**: Unusual patterns, outliers, or unexpected behaviors
6. **Performance Insights**: Response times, throughput, bottlenecks if applicable
7. **Actionable Recommendations**: Specific next steps to investigate or fix issues

Use the available tools to:
- Install any needed analysis libraries (pandas, matplotlib, etc.)
- Read and parse the log file
- Perform statistical analysis
- Generate insights and recommendations

Focus on practical, actionable findings that would help a developer debug issues.
""".strip()

MAX_ITERATIONS = 15  # Increased for complex log analysis


def read_local_file(file_path: str) -> str:
    """Read a local file and return its contents."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except Exception as e:
        raise ValueError(f"Failed to read file {file_path}: {e}")


def estimate_tokens(text: str) -> int:
    """Rough estimation of token count (1 token ≈ 4 characters)."""
    return len(text) // 4


def smart_chunk_log(log_contents: str, max_tokens: int = 25000) -> str:
    """
    Intelligently chunk large log files by extracting key sections.
    For very large files, sample different time periods and error patterns.
    """
    estimated_tokens = estimate_tokens(log_contents)
    
    if estimated_tokens <= max_tokens:
        return log_contents
    
    logger.info(f"Large file detected ({estimated_tokens:,} estimated tokens). Creating intelligent sample...")
    
    lines = log_contents.split('\n')
    total_lines = len(lines)
    
    # Strategy: Take beginning, end, and sample middle sections + all errors
    sample_size = max_tokens * 4  # Convert back to characters
    
    # Take first 20% of target size
    beginning_size = sample_size // 5
    beginning_chars = 0
    beginning_lines = []
    for line in lines[:total_lines//3]:
        if beginning_chars + len(line) > beginning_size:
            break
        beginning_lines.append(line)
        beginning_chars += len(line)
    
    # Take last 20% of target size  
    end_size = sample_size // 5
    end_chars = 0
    end_lines = []
    for line in reversed(lines[-total_lines//3:]):
        if end_chars + len(line) > end_size:
            break
        end_lines.insert(0, line)
        end_chars += len(line)
    
    # Extract all error/warning lines (remaining 60% of budget)
    error_keywords = ['error', 'ERROR', 'warn', 'WARN', 'fail', 'FAIL', 'exception', 'EXCEPTION', '404', '500', 'timeout']
    error_lines = []
    error_chars = 0
    error_budget = sample_size - beginning_chars - end_chars
    
    for line in lines:
        if any(keyword in line.lower() for keyword in error_keywords):
            if error_chars + len(line) > error_budget:
                break
            error_lines.append(line)
            error_chars += len(line)
    
    # Combine sections with clear separators
    sampled_content = f"""=== BEGINNING OF LOG ({len(beginning_lines)} lines) ===
{chr(10).join(beginning_lines)}

=== ERRORS AND WARNINGS ({len(error_lines)} lines) ===
{chr(10).join(error_lines)}

=== END OF LOG ({len(end_lines)} lines) ===
{chr(10).join(end_lines)}

=== SAMPLING INFO ===
Original file: {total_lines:,} lines
Sampled: {len(beginning_lines + error_lines + end_lines):,} lines
Estimated original tokens: {estimated_tokens:,}
Estimated sample tokens: {estimate_tokens(chr(10).join(beginning_lines + error_lines + end_lines)):,}
"""
    
    return sampled_content


def run_log_detective(runloop: Runloop, devbox: DevboxView, openai_api_key: str, log_file_path: str):
    """Run the log detective agent on a specific log file."""
    openai.api_key = openai_api_key
    
    # Read the local log file
    logger.info(f"Reading local log file: {log_file_path}")
    try:
        log_contents = read_local_file(log_file_path)
        file_size_mb = len(log_contents) / (1024 * 1024)
        logger.info(f"Log file size: {file_size_mb:.2f} MB")
        
        # Apply intelligent chunking for large files
        processed_contents = smart_chunk_log(log_contents)
        if processed_contents != log_contents:
            logger.info("Applied intelligent sampling for large file analysis")
            
    except Exception as e:
        logger.error(f"Failed to read log file: {e}")
        return
    
    # Extract filename for the prompt
    filename = os.path.basename(log_file_path)
    
    @ell.tool()
    def execute_shell_command(command: str):
        """Run a shell command in the devbox."""
        result = runloop.devboxes.execute_sync(devbox.id, command=command)
        return result.stdout

    @ell.tool()
    def read_file(filename: str):
        """Reads a file on the devbox."""
        return runloop.devboxes.read_file_contents(devbox.id, file_path=filename)

    @ell.tool()
    def write_file(filename: str, contents: str):
        """Writes a file on the devbox."""
        runloop.devboxes.write_file_contents(
            devbox.id, file_path=filename, contents=contents
        )

    @ell.complex(
        model="gpt-4-turbo", 
        tools=[execute_shell_command, read_file, write_file], 
        client=openai.OpenAI(api_key=openai_api_key)
    )
    def invoke_detective(message_history: List[Message]):
        """Calls the LLM to analyze the log file."""
        if not message_history:
            logger.debug("invoke_detective: first call")
        else:
            logger.debug(f"invoke_detective: continuing analysis...")
            
        messages = [
            ell.system(SYSTEM_PROMPT),
            ell.user(USER_PROMPT_TEMPLATE.format(filename=filename)),
        ] + message_history
        return messages

    # Upload the processed log file to the devbox
    logger.info(f"Uploading log file to devbox as '{filename}'...")
    write_file(filename, processed_contents)
    
    # Start the analysis
    logger.info("Starting log analysis...")
    message_history = []
    result = invoke_detective(message_history)
    num_iterations = 0
    
    while result.tool_calls and num_iterations < MAX_ITERATIONS:
        logger.debug(f"Iteration {num_iterations + 1}: performing tool calls")
        message_history.append(result)
        result_message = result.call_tools_and_collect_as_message()
        logger.debug(f"Tool results received")
        message_history.append(result_message)
        result = invoke_detective(message_history)
        num_iterations += 1
    
    # Output the final analysis
    logger.info("Analysis complete!")
    print("\n" + "="*80)
    print(f"LOG DETECTIVE ANALYSIS: {filename}")
    print("="*80)
    print(result.text)
    print("="*80 + "\n")


def main(openai_api_key: str, runloop_api_key: str, log_file_path: str):
    """Main function to run the log detective."""
    runloop = Runloop(bearer_token=runloop_api_key)

    logger.info("Creating devbox for log analysis...")
    devbox = runloop.devboxes.create_and_await_running(create_args={})
    devbox_id = devbox.id
    
    try:
        run_log_detective(runloop, devbox, openai_api_key, log_file_path)
    finally:
        logger.info(f"Destroying devbox {devbox_id}...")
        runloop.devboxes.shutdown(devbox_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    # Check for log file argument
    if len(sys.argv) != 2:
        print("Usage: python log_detective_agent.py <path_to_log_file>")
        print("\nExample:")
        print("python log_detective_agent.py /path/to/your/logs/main.log")
        sys.exit(1)
    
    log_file_path = sys.argv[1]
    
    # Validate file exists
    if not os.path.exists(log_file_path):
        print(f"Error: Log file '{log_file_path}' not found")
        sys.exit(1)
    
    logger.info(f"Starting Log Detective Agent")
    logger.info(f"Target log file: {log_file_path}")

    # Get API keys
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY is not set")
    runloop_api_key = os.environ.get("RUNLOOP_API_KEY")
    if not runloop_api_key:
        raise ValueError("RUNLOOP_API_KEY is not set")

    main(openai_api_key, runloop_api_key, log_file_path)