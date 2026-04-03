import re

with open('docker-compose.yml', 'r', encoding='utf-8') as f:
    content = f.read()

# Add depends_on for interview-agent
content = re.sub(
    r'(  interview-agent:\n    build:.*?\n    container_name: interview-agent\n)',
    r'\1    depends_on:\n      redis:\n        condition: service_healthy\n',
    content,
    flags=re.DOTALL
)

# Add depends_on for cleanup-service
content = re.sub(
    r'(  cleanup-service:\n    build:.*?\n    container_name: cleanup-service\n)',
    r'\1    depends_on:\n      redis:\n        condition: service_healthy\n',
    content,
    flags=re.DOTALL
)

# Add depends_on for meeting-bot
content = re.sub(
    r'(  meeting-bot:\n    build:.*?\n    container_name: meeting-bot\n)',
    r'\1    depends_on:\n      redis:\n        condition: service_healthy\n',
    content,
    flags=re.DOTALL
)

# Add depends_on for tts (already has meeting-bot dependency)
content = re.sub(
    r'(  tts:\n    build:.*?\n    container_name: tts\n    depends_on:\n      meeting-bot:\n        condition: service_healthy)',
    r'\1\n      redis:\n        condition: service_healthy',
    content,
    flags=re.DOTALL
)

# Add depends_on for ui
content = re.sub(
    r'(  ui:\n    build:.*?\n    container_name: ui\n)',
    r'\1    depends_on:\n      redis:\n        condition: service_healthy\n',
    content,
    flags=re.DOTALL
)

with open('docker-compose.yml', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ Added Redis dependency to all services')
