# AWS VPS Setup Guide — UpDown Bot Phase 2

## What we're building
Deploy the UpDown bot to AWS us-east-1 (Virginia) for low-latency access to:
- Polymarket CLOB API (~5ms from us-east-1)
- Binance WebSocket (~1ms from us-east-1)

## Step 1: Launch EC2 Instance

1. Go to **AWS Console → EC2 → Launch Instance**
2. **Name**: `polymarket-updown`
3. **AMI**: **Ubuntu 24.04 LTS** (ami-ubuntu24.04)
4. **Instance type**: **t3.micro** (2 vCPU, 1GB RAM — $0.0116/hr ≈ $8.50/mo)
   - If you need more RAM later, upgrade to t3.small ($17/mo)
5. **Key pair**: Create new key pair
   - Name: `updown-bot-key`
   - Type: RSA
   - Format: **.pem** (for SSH) — also save .ppk if you use PuTTY
   - ⚠️ **Download the .pem file** — this is your ONLY chance to download it
6. **Network settings**: 
   - ✅ Allow SSH traffic from: **My IP** (NOT 0.0.0.0/0)
   - Leave other ports closed (we don't need HTTP/HTTPS)
7. **Storage**: 8GB gp3 (default is fine)
8. Click **Launch Instance**

## Step 2: Fix Security Group (if SSH doesn't work)

This is the most common issue — the security group may not allow your IP.

1. Go to **EC2 → Security Groups**
2. Find the security group attached to your instance (named `launch-wizard-1` or similar)
3. Click **Inbound rules → Edit inbound rules**
4. You should see:
   - Type: SSH | Port: 22 | Source: your-ip/32
5. If the source is blank or wrong:
   - Add rule: Type = **SSH**, Port = **22**, Source = **My IP**
   - Click **Save rules**
6. If you have a dynamic IP and it changed since launch, update the source

## Step 3: SSH Into Your Instance

1. Find your instance's **Public IPv4 address** in EC2 → Instances
2. Move your .pem file and set permissions:
   ```bash
   chmod 600 ~/Downloads/updown-bot-key.pem
   ```
3. SSH in (Ubuntu AMI uses `ubuntu` as the login user):
   ```bash
   ssh -i ~/Downloads/updown-bot-key.pem ubuntu@<YOUR-INSTANCE-IP>
   ```
4. Say **yes** to the fingerprint prompt on first connect

## Step 4: Install Dependencies

Once SSH'd in, run these commands:

```bash
# System updates
sudo apt update && sudo apt upgrade -y

# Python 3.12+ and venv
sudo apt install -y python3 python3-venv python3-pip git

# Create bot directory
mkdir -p ~/polymarket-updown/data

# Clone the repo
cd ~
git clone https://github.com/easyd24/polymarket-updown.git polymarket-updown

# Create virtual environment
cd ~/polymarket-updown
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Step 5: Configure the Bot

```bash
# Copy .env template and edit with your keys
cd ~/polymarket-updown
cp .env.example .env  # or create from scratch
nano .env
```

Add these to `.env`:
```
TELEGRAM_BOT_TOKEN=<your-updown-bot-token>
TELEGRAM_CHAT_ID=6104346726
POLYMARKET_PRIVATE_KEY=<your-separate-wallet-key>
DEPOSIT_WALLET=<deposit-wallet-address>
EOA_WALLET=<eoa-wallet-address>
POLYMARKET_API_KEY=<api-key>
POLYMARKET_API_SECRET=<api-secret>
POLYMARKET_API_PASSPHRASE=<api-passphrase>
```

```bash
chmod 600 .env
```

## Step 6: Enable 5m Markets

Edit `config.py` to enable 5-minute markets (they need AWS latency):

```bash
nano config.py
# Change "enabled": False to True for the 5m series
```

## Step 7: Create Systemd Service

```bash
sudo nano /etc/systemd/system/polymarket-updown.service
```

Paste:
```ini
[Unit]
Description=Polymarket UpDown Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/polymarket-updown
ExecStart=/home/ubuntu/polymarket-updown/venv/bin/python main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable polymarket-updown
sudo systemctl start polymarket-updown
```

## Step 8: Verify

```bash
# Check it's running
sudo systemctl status polymarket-updown

# Check logs
sudo journalctl -u polymarket-updown -f

# Test latency
ping -c 3 clob.polymarket.com
ping -c 3 api.binance.com
```

Expected latencies from us-east-1:
- Polymarket CLOB: ~5-10ms
- Binance API: ~1-3ms

## Step 9: Lock Down SSH (Optional but Recommended)

```bash
# Disable password auth
sudo nano /etc/ssh/sshd_config
# Set: PasswordAuthentication no
# Set: PubkeyAuthentication yes

sudo systemctl restart sshd
```

## Troubleshooting

### Can't SSH in
1. Check security group allows SSH from YOUR IP (not 0.0.0.0/0 unless desperate)
2. Use `ubuntu` as username (not `ec2-user` or `root`)
3. Check .pem file has `chmod 600`
4. Check instance is "running" and passed both status checks
5. Try: `ssh -v -i key.pem ubuntu@IP` for verbose debug

### Instance too slow
- t3.micro = 1GB RAM. If bot crashes with OOM:
- Upgrade to t3.small (2GB RAM, ~$17/mo)

### Key pair lost
- Can't be recovered. Create a new key pair and new instance.

### IP changes after reboot
- Use Elastic IP (free while attached to running instance)
- EC2 → Elastic IPs → Allocate → Associate with instance