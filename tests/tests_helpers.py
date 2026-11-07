from email.message import EmailMessage


def raw_mail(subject, sender, to, date, mid, body="검토 요청드립니다."):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Date"], m["Message-ID"] = subject, sender, to, date, mid
    m.set_content(body)
    return bytes(m)
