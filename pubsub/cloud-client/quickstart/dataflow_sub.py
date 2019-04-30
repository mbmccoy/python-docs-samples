#!/usr/bin/env python
import argparse
import logging
import time

import re

from google.cloud import pubsub_v1

import apache_beam as beam
from apache_beam.io import ReadFromText
from apache_beam.io import WriteToText
from apache_beam.metrics import Metrics
from apache_beam.metrics.metric import MetricsFilter
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions
from typing import List, AnyStr


# noinspection PyAbstractClass
class WordExtractingDoFn(beam.DoFn):
    """Parse each line of input text into words."""

    def __init__(self):
        super(WordExtractingDoFn, self).__init__()
        self.words_counter = Metrics.counter(self.__class__, 'words')
        self.word_lengths_counter = Metrics.counter(self.__class__, 'word_lengths')
        self.word_lengths_dist = Metrics.distribution(
            self.__class__, 'word_len_dist')
        self.empty_line_counter = Metrics.counter(self.__class__, 'empty_lines')

    def process(self, element, **kwargs):
        """Returns an iterator over the words of this element.
    The element is a line of text.  If the line is blank, note that, too.
    Args:
      element: the element being processed
    Returns:
      The processed element.
    """
        text_line = element.strip()
        if not text_line:
            self.empty_line_counter.inc(1)
        words = re.findall(r'[\w\']+', text_line, re.UNICODE)
        for w in words:
            self.words_counter.inc()
            self.word_lengths_counter.inc(len(w))
            self.word_lengths_dist.update(len(w))
        return words


def dataflow_sub(project_id, subscription_name):
    """Receives messages from a Pub/Sub subscription."""
    # [START pubsub_quickstart_sub_client]
    # Initialize a Subscriber client
    client = pubsub_v1.SubscriberClient()
    # [END pubsub_quickstart_sub_client]
    # Create a fully qualified identifier in the form of
    # `projects/{project_id}/subscriptions/{subscription_name}`
    subscription_path = client.subscription_path(
        project_id, subscription_name)

    def callback(message):
        print('Received message {} of message ID {}'.format(
            message, message.message_id))
        # Acknowledge the message. Unack'ed messages will be redelivered.
        message.ack()
        print('Acknowledged message of message ID {}\n'.format(
            message.message_id))

    client.subscribe(subscription_path, callback=callback)
    print('Listening for messages on {}..\n'.format(subscription_path))

    # Keep the main thread from exiting so the subscriber can
    # process messages in the background.
    while True:
        time.sleep(60)


def build_pipeline(project_id,  # type: AnyStr
                   input_subscription,  # type: AnyStr
                   output_subscription,  # type: AnyStr
                   pipeline_args,  # type: List[AnyStr]
                   ):
    # type: (...) -> beam.Pipeline

    # We use the save_main_session option because one or more DoFn's in this
    # workflow rely on global context (e.g., a module imported at module level).
    pipeline_options = PipelineOptions(pipeline_args)
    pipeline_options.view_as(SetupOptions).save_main_session = True
    p = beam.Pipeline(options=pipeline_options)

    # Read the text file[pattern] into a PCollection.
    messages = p | 'read' >> beam.io.ReadFromPubSub(topic=input_subscription)

    # Count the occurrences of each word.
    def count_ones(word_ones):
        (word, ones) = word_ones
        return word, sum(ones)

    counts = (messages
              | 'split' >> (beam.ParDo(WordExtractingDoFn())
                            .with_output_types(unicode))
              | 'pair_with_one' >> beam.Map(lambda x: (x, 1))
              | 'group' >> beam.GroupByKey()
              | 'count' >> beam.Map(count_ones))

    # Format the counts into a PCollection of strings.
    def format_result(word_count):
        (word, count) = word_count
        return '%s: %d' % (word, count)

    output = counts | 'format' >> beam.Map(format_result)

    # Write to Pub/Sub
    output | beam.io.WriteStringsToPubSub(known_args.output_topic)

    return p


def main(argv=None):
    """Main entry point; defines and runs the wordcount pipeline."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--input',
                        dest='input',
                        default='gs://dataflow-samples/shakespeare/kinglear.txt',
                        help='Input file to process.')
    parser.add_argument('--output',
                        dest='output',
                        required=True,
                        help='Output file to write results to.')

    parser.add_argument('project_id', help='Google Cloud project ID')
    parser.add_argument('subscription_name', help='Pub/Sub subscription name')

    known_args, pipeline_args = parser.parse_known_args(argv)

    dataflow_sub(known_args.project_id, pipeline_args.subscription_name)

    p = build_pipeline(
        project_id=known_args.project_id,
        input_subscription=known_args.subscription_name,
        output_subscription=known_args.output,
        pipeline_args=pipeline_args,
    )

    result = p.run()
    result.wait_until_finish()

    # Do not query metrics when creating a template which doesn't run
    if (not hasattr(result, 'has_job')  # direct runner
            or result.has_job):  # not just a template creation
        empty_lines_filter = MetricsFilter().with_name('empty_lines')
        query_result = result.metrics().query(empty_lines_filter)
        if query_result['counters']:
            empty_lines_counter = query_result['counters'][0]
            logging.info('number of empty lines: %d', empty_lines_counter.result)

        word_lengths_filter = MetricsFilter().with_name('word_len_dist')
        query_result = result.metrics().query(word_lengths_filter)
        if query_result['distributions']:
            word_lengths_dist = query_result['distributions'][0]
            logging.info('average word length: %d', word_lengths_dist.result.mean)


if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)
    main()
