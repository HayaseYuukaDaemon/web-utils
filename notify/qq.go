package notify

import (
	"bytes"
	"context"
	"crypto/tls"
	"fmt"
	"mime"
	"mime/quotedprintable"
	"net"
	"net/smtp"
	"time"
	"uuid"
)

type notifierQQ struct {
	instanceID string
	dialer     *net.Dialer

	Mail     string
	AuthCode string
}

func (n *notifierQQ) Type() NotifyType {
	return NotifyTypeQQ
}

func (n *notifierQQ) InstanceID() string {
	return n.instanceID
}

func (n *notifierQQ) Desc() string {
	return fmt.Sprintf("send qq email to %s by itself", n.Mail)
}

func (n *notifierQQ) Notify(ctx context.Context, req NotifyRequest) error {
	const host = "smtp.qq.com"

	dialer := tls.Dialer{
		NetDialer: n.dialer,
		Config: &tls.Config{
			ServerName: host,
			MinVersion: tls.VersionTLS12,
		},
	}

	// ctx 控制 TCP 建连和 TLS 握手。
	conn, err := dialer.DialContext(
		ctx,
		"tcp",
		net.JoinHostPort(host, "465"),
	)
	if err != nil {
		return err
	}
	defer conn.Close()

	// 连接建立后，继续监听取消，打断后续 SMTP 读写。
	stop := context.AfterFunc(ctx, func() {
		_ = conn.Close()
	})
	defer stop()

	client, err := smtp.NewClient(conn, host)
	if err != nil {
		return err
	}
	defer client.Close()

	// password 填 QQ 邮箱授权码。
	auth := smtp.PlainAuth("", n.Mail, n.AuthCode, host)
	if err := client.Auth(auth); err != nil {
		return err
	}

	if err := client.Mail(n.Mail); err != nil {
		return err
	}
	if err := client.Rcpt(n.Mail); err != nil {
		return err
	}

	writer, err := client.Data()
	if err != nil {
		return err
	}

	var msg bytes.Buffer

	// 邮件头与正文之间必须有空行，换行使用 \r\n。
	fmt.Fprintf(&msg, "From: %s\r\n", n.Mail)
	fmt.Fprintf(&msg, "To: %s\r\n", n.Mail)
	fmt.Fprintf(&msg, "Date: %s\r\n", time.Now().Format(time.RFC1123Z))
	fmt.Fprintf(&msg, "Subject: %s\r\n", mime.QEncoding.Encode("UTF-8", "[Notify] "+req.Title))
	msg.WriteString("MIME-Version: 1.0\r\n")
	msg.WriteString("Content-Type: text/plain; charset=UTF-8\r\n")
	msg.WriteString("Content-Transfer-Encoding: quoted-printable\r\n")
	msg.WriteString("\r\n")

	body := quotedprintable.NewWriter(&msg)
	if _, err := body.Write([]byte(req.Body)); err != nil {
		return err
	}
	if err := body.Close(); err != nil {
		return err
	}

	if _, err := writer.Write(msg.Bytes()); err != nil {
		return err
	}
	// Close 会结束邮件数据，并等待服务器确认接受。
	if err := writer.Close(); err != nil {
		return err
	}

	return client.Quit()
}

func NewQQNotifier(mail string, authCode string, instanceID string) *notifierQQ {
	return &notifierQQ{
		dialer: &net.Dialer{},
		instanceID: func() string {
			if instanceID == "" {
				return uuid.New().String()
			}
			return instanceID
		}(),
		Mail:     mail,
		AuthCode: authCode,
	}
}

func (n *notifierQQ) WithDialer(dialer *net.Dialer) *notifierQQ {
	n.dialer = dialer
	return n
}
