from email.message import Message
from botocore.exceptions import ClientError
import boto3
import email
import os
import uuid
from bs4 import BeautifulSoup
import sys
from email import policy

workmail_message_flow = boto3.client('workmailmessageflow')
s3 = boto3.client('s3')


def lambda_handler(event, context):
    """

    Hello world example for AWS WorkMail

    Parameters
    ----------
    event: dict, required
        AWS WorkMail Message Summary Input Format
        For more information, see https://docs.aws.amazon.com/workmail/latest/adminguide/lambda.html

        {
            "summaryVersion": "2019-07-28",                              # AWS WorkMail Message Summary Version
            "envelope": {
                "mailFrom" : {
                    "address" : "from@domain.test"                       # String containing from email address
                },
                "recipients" : [                                         # List of all recipient email addresses
                   { "address" : "recipient1@domain.test" },
                   { "address" : "recipient2@domain.test" }
                ]
            },
            "sender" : {
                "address" :  "sender@domain.test"                        # String containing sender email address
            },
            "subject" : "Hello From Amazon WorkMail!",                   # String containing email subject (Truncated to first 256 chars)"
            "messageId": "00000000-0000-0000-0000-000000000000",         # String containing message id for retrieval using workmail flow API
            "invocationId": "00000000000000000000000000000000",          # String containing the id of this lambda invocation. Useful for detecting retries and avoiding duplication
            "flowDirection": "INBOUND",                                  # String indicating direction of email flow. Value is either "INBOUND" or "OUTBOUND"
            "truncated": false                                           # boolean indicating if any field in message was truncated due to size limitations
        }

    context: object, required
        Lambda Context runtime methods and attributes
        https://docs.aws.amazon.com/lambda/latest/dg/python-context-object.html

    Returns
    -------
    Amazon WorkMail Sync Lambda Response Format. For more information, see https://docs.aws.amazon.com/workmail/latest/adminguide/lambda.html#synchronous-schema
        return {
          'actions': [                                              # Required, should contain at least 1 list element
          {
            'action' : {                                            # Required
              'type': 'string',                                     # Required. For example: "BOUNCE", "DEFAULT". For full list of valid values, see https://docs.aws.amazon.com/workmail/latest/adminguide/lambda.html#synchronous-schema
              'parameters': { <various> }                           # Optional. For bounce, <various> can be {"bounceMessage": "message that goes in bounce mail"}
            },
            'recipients': list of strings,                          # Optional if allRecipients is present. Indicates list of recipients for which this action applies.
            'allRecipients': boolean                                # Optional if recipients is present. Indicates whether this action applies to all recipients
          }
        ]}

    """
    
    sys.setrecursionlimit(1500)

    from_address = event['envelope']['mailFrom']['address']
    subject = event['subject']
    flow_direction = event['flowDirection']
    message_id = event['messageId']
    print(f"Received email with message ID {message_id}, flowDirection {flow_direction}, from {from_address} with Subject {subject}")

    try:
        raw_msg = workmail_message_flow.get_raw_message_content(messageId=message_id)
        email_generation_policy = policy.SMTP.clone(refold_source='none')
        parsed_msg = email.message_from_bytes(raw_msg['messageContent'].read(), policy=email_generation_policy)

        # Updating subject. For more examples, see https://github.com/aws-samples/amazon-workmail-lambda-templates.
        # parsed_msg.replace_header('Subject', f"[Hello World!] {subject}")
        
        updated_email = update_email_body(parsed_msg)
        
        # Try to get the email bucket.
        updated_email_bucket_name = os.getenv('UPDATED_EMAIL_S3_BUCKET')
        if not updated_email_bucket_name:
            print('UPDATED_EMAIL_S3_BUCKET not set in environment. '
                  'Please follow https://docs.aws.amazon.com/lambda/latest/dg/env_variables.html to set it.')
            return

        key = str(uuid.uuid4())

        # Put the message in S3, so WorkMail can access it.
        s3.put_object(Body=updated_email.as_bytes(), Bucket=updated_email_bucket_name, Key=key)

        # Update the email in WorkMail.
        s3_reference = {
            'bucket': updated_email_bucket_name,
            'key': key
        }
        content = {
            's3Reference': s3_reference
        }

        assert content  # Silence pyflakes for unused variable

        # If you'd like to finalise modifying email subjects, then uncomment the line below.
        workmail_message_flow.put_raw_message_content(messageId=message_id, content=content)

    except ClientError as e:
        if e.response['Error']['Code'] == 'MessageFrozen':
            # Redirect emails are not eligible for update, handle it gracefully.
            print(f"Message {message_id} is not eligible for update. This is usually the case for a redirected email")
        else:
            # Send some context about this error to Lambda Logs
            print(e)
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                print(f"Message {message_id} does not exist. Messages in transit are no longer accessible after 1 day")
            elif e.response['Error']['Code'] == 'InvalidContentLocation':
                print('WorkMail could not access the updated email content. See https://docs.aws.amazon.com/workmail/latest/adminguide/update-with-lambda.html')
            raise(e)

    # Return value is ignored when Lambda is configured asynchronously at Amazon WorkMail
    # For more information, see https://docs.aws.amazon.com/workmail/latest/adminguide/lambda.html
    return {
        'actions': [
            {
                'allRecipients': True,  # For all recipients
                'action': {'type': 'DEFAULT'}  # let the email be sent normally
            }
        ]
    }

def update_text_content(part):
    """
    Updates "text/plain" email body part with translated body.
    Parameters
    ----------
    parsed_email: email.message.Message, required
        EmailMessage representation the downloaded email
    Returns
    -------
    email.message.Message
        EmailMessage representation the translated email
    """
    text_content = part.get_content()
    # text_content = text_content + "\n\n" + translated_body
    return text_content

def update_html_content(part):
    """
    Updates "text/html" email body part with translated body.
    Parameters
    ----------
    parsed_email: email.message.Message, required
        EmailMessage representation the downloaded email
    Returns
    -------
    email.message.Message
        EmailMessage representation the translated email
    """
    html_content = part.get_content()
    soup = BeautifulSoup(html_content, "html.parser")
    
    ok_prefixes = ["https://jdami.co", "http://jdami.co", "https://prova.design", "http://prova.design", "https://jonathandamico.me", "http://jonathandamico.me"]
    
    for link in soup.find_all('a'):
        
        link_should_be_changed = True
        
        for prefix in ok_prefixes:
            if(link.get('href').startswith(prefix)):
                link_should_be_changed = False
                break
        
        trim_length = 40
        if link.string is not None and link_should_be_changed:
            if len(link.get('href')) > trim_length:
                link.string = link.string + ' [' + link.get('href')[:trim_length] + '...]'
            else:
                link.string = link.string + ' [' + link.get('href') + ']'

    return soup

def update_email_body(parsed_email):
    """
    Finds and updates the "text/html" and "text/plain" email body parts.
    Parameters
    ----------
    parsed_email: email.message.Message, required
        EmailMessage representation the downloaded email
    Returns
    -------
    email.message.Message
        EmailMessage representation the updated email
    """
    text_charset = None
    if parsed_email.is_multipart():
        # Walk over message parts of this multipart email.
        for part in parsed_email.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get_content_disposition())
            if content_type == 'text/plain' and 'attachment' not in content_disposition:
                transfer_encoding = part['Content-Transfer-Encoding']
                text_charset = part.get_content_charset()
                new_text_body = update_text_content(part)
                part.set_content(new_text_body, "plain", cte=transfer_encoding)
                part.set_charset(text_charset)
            elif content_type == 'text/html' and 'attachment' not in content_disposition:
                transfer_encoding = part['Content-Transfer-Encoding']
                html_charset = part.get_content_charset()
                new_html_body = update_html_content(part)
                if new_html_body is not None:
                    part.set_content(new_html_body.encode(html_charset), "text", "html", cte=transfer_encoding)
                    part.set_charset(html_charset)
    else:
        # Its a plain email with text/plain body
        transfer_encoding = parsed_email['Content-Transfer-Encoding']
        text_charset = parsed_email.get_content_charset()
        new_text_body = update_text_content(parsed_email)
        parsed_email.set_content(new_text_body, "plain", charset=text_charset, cte=transfer_encoding)
    return parsed_email
