import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

EMAIL_SENDER   = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

GREEN      = "#2D7A3A"
GREEN_LIGHT = "#EBF5ED"
GRAY       = "#F7F7F7"
DARK       = "#1A1A1A"
MUTED      = "#666666"
BORDER     = "#E0E0E0"


def format_html(data: dict, transcript: str) -> str:
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = data.get("title", "შეხვედრის შეჯამება")

    def section(label, items, is_list=True):
        if not items:
            return ""
        rows = ""
        if is_list:
            for item in items:
                rows += f'<li style="margin-bottom:8px; color:{DARK};">{item}</li>'
            content = f'<ul style="margin:0; padding-left:20px;">{rows}</ul>'
        else:
            content = f'<p style="margin:0; color:{DARK}; line-height:1.7;">{items}</p>'
        return f"""
        <div style="margin-bottom:24px;">
            <div style="font-size:11px; font-weight:600; color:{GREEN}; 
                        text-transform:uppercase; letter-spacing:0.08em; 
                        margin-bottom:10px; border-bottom:2px solid {GREEN}; 
                        padding-bottom:6px;">
                {label}
            </div>
            {content}
        </div>"""

    # Action items
    action_rows = ""
    for item in data.get("action_items", []):
        owner    = item.get("owner", "TBD")
        task     = item.get("task", "")
        deadline = item.get("deadline", "TBD")
        action_rows += f"""
        <tr>
            <td style="padding:10px 12px; border-bottom:1px solid {BORDER}; 
                       color:{DARK}; font-size:14px;">{task}</td>
            <td style="padding:10px 12px; border-bottom:1px solid {BORDER}; 
                       color:{MUTED}; font-size:13px; white-space:nowrap;">{owner}</td>
            <td style="padding:10px 12px; border-bottom:1px solid {BORDER}; 
                       color:{MUTED}; font-size:13px; white-space:nowrap;">{deadline}</td>
        </tr>"""

    action_section = ""
    if data.get("action_items"):
        action_section = f"""
        <div style="margin-bottom:24px;">
            <div style="font-size:11px; font-weight:600; color:{GREEN}; 
                        text-transform:uppercase; letter-spacing:0.08em; 
                        margin-bottom:10px; border-bottom:2px solid {GREEN}; 
                        padding-bottom:6px;">
                დავალებები
            </div>
            <table style="width:100%; border-collapse:collapse; 
                          border:1px solid {BORDER}; border-radius:6px; 
                          overflow:hidden; font-size:14px;">
                <thead>
                    <tr style="background:{GREEN_LIGHT};">
                        <th style="padding:10px 12px; text-align:left; 
                                   color:{GREEN}; font-size:12px; 
                                   font-weight:600;">დავალება</th>
                        <th style="padding:10px 12px; text-align:left; 
                                   color:{GREEN}; font-size:12px; 
                                   font-weight:600;">პასუხისმგებელი</th>
                        <th style="padding:10px 12px; text-align:left; 
                                   color:{GREEN}; font-size:12px; 
                                   font-weight:600;">ვადა</th>
                    </tr>
                </thead>
                <tbody>{action_rows}</tbody>
            </table>
        </div>"""

    unresolved_section = ""
    if data.get("unresolved"):
        items_html = "".join([
            f'<li style="margin-bottom:8px; color:{DARK};">{u}</li>'
            for u in data.get("unresolved", [])
        ])
        unresolved_section = f"""
        <div style="margin-bottom:24px; background:#FFF8E7; 
                    border-left:4px solid #F0A500; 
                    border-radius:4px; padding:16px;">
            <div style="font-size:11px; font-weight:600; color:#B07800; 
                        text-transform:uppercase; letter-spacing:0.08em; 
                        margin-bottom:10px;">
                გადაუჭრელი საკითხები
            </div>
            <ul style="margin:0; padding-left:20px;">
                {items_html}
            </ul>
        </div>"""

    decisions = data.get("decisions", [])
    decisions_html = section("გადაწყვეტილებები", decisions)
    summary_html   = section("შეჯამება", data.get("summary", ""), is_list=False)

    transcript_lines = transcript.replace("\n", "<br>")

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0; padding:0; background:{GRAY}; 
             font-family: Georgia, 'Times New Roman', serif;">

  <table width="100%" cellpadding="0" cellspacing="0" 
         style="background:{GRAY}; padding:32px 16px;">
    <tr><td align="center">
      <table width="620" cellpadding="0" cellspacing="0" 
             style="background:#ffffff; border-radius:8px; 
                    overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:{GREEN}; padding:28px 36px;">
            <div style="font-size:13px; color:rgba(255,255,255,0.7); 
                        margin-bottom:4px; letter-spacing:0.05em;">
              ქართული აგრო სახლი
            </div>
            <div style="font-size:22px; color:#ffffff; font-weight:600; 
                        line-height:1.3;">
              {title}
            </div>
            <div style="font-size:13px; color:rgba(255,255,255,0.7); 
                        margin-top:8px;">
              {now}
            </div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:32px 36px;">
            {summary_html}
            {decisions_html}
            {action_section}
            {unresolved_section}
          </td>
        </tr>

        <!-- Transcript -->
        <tr>
          <td style="padding:0 36px 32px;">
            <details>
              <summary style="cursor:pointer; color:{MUTED}; font-size:13px; 
                              padding:12px 16px; background:{GRAY}; 
                              border-radius:6px; user-select:none;">
                სრული ტრანსკრიფცია (დასაჭერია გასახსნელად)
              </summary>
              <div style="padding:16px; background:{GRAY}; border-radius:6px; 
                          margin-top:8px; font-size:13px; color:{MUTED}; 
                          line-height:1.8;">
                {transcript_lines}
              </div>
            </details>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:{GREEN_LIGHT}; padding:16px 36px; 
                     border-top:1px solid {BORDER};">
            <div style="font-size:12px; color:{MUTED}; text-align:center;">
              ეს შეჯამება გენერირებულია ავტომატურად AI-ის მეშვეობით · 
              Georgian Agro House · info@gah.ge
            </div>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>

</body>
</html>"""
    return html


def send(data: dict, transcript: str, recipients: list):
    print(f"[Email] Sending to {len(recipients)} recipient(s)...")
    title   = data.get("title", "შეხვედრის შეჯამება")
    date    = datetime.now().strftime("%Y-%m-%d")
    subject = f"შეხვედრა: {title} — {date}"

    html_body = format_html(data, transcript)

    msg = MIMEMultipart("alternative")
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, recipients, msg.as_string())

    print(f"[Email] Sent to: {', '.join(recipients)}")