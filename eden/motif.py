#!/usr/bin/env python

import sys
import os
import random
import re
from time import time, clock
import multiprocessing as mp
import numpy as np
from itertools import tee, chain
from collections import defaultdict

import joblib

from eden import apply_async
from eden.graph import Vectorizer
from eden.path import Vectorizer as PathVectorizer
from eden.util import vectorize, mp_pre_process, compute_intervals
from eden.converter.fasta import sequence_to_eden
from eden.modifier.seq import seq_to_seq, shuffle_modifier
from eden.util import fit
from eden.util.iterated_maximum_subarray import compute_max_subarrays

import esm

import logging
logger = logging.getLogger(__name__)


class SequenceMotif(object):

    def __init__(self,
                 min_subarray_size=7,
                 max_subarray_size=10,
                 min_motif_count=1,
                 min_cluster_size=1,
                 training_size=None,
                 negative_ratio=2,
                 shuffle_order=2,
                 n_iter_search=1,
                 complexity=4,
                 nbits=20,
                 clustering_algorithm=None,
                 n_jobs=4,
                 n_blocks=8,
                 block_size=None,
                 pre_processor_n_jobs=4,
                 pre_processor_n_blocks=8,
                 pre_processor_block_size=None,
                 random_state=1):
        self.n_jobs = n_jobs
        self.n_blocks = n_blocks
        self.block_size = block_size
        self.pre_processor_n_jobs = pre_processor_n_jobs
        self.pre_processor_n_blocks = pre_processor_n_blocks
        self.pre_processor_block_size = pre_processor_block_size
        self.training_size = training_size
        self.n_iter_search = n_iter_search
        self.complexity = complexity
        self.nbits = nbits
        # init vectorizer
        self.vectorizer = Vectorizer(complexity=self.complexity, nbits=self.nbits)
        self.seq_vectorizer = PathVectorizer(complexity=self.complexity, nbits=self.nbits)
        self.negative_ratio = negative_ratio
        self.shuffle_order = shuffle_order
        self.clustering_algorithm = clustering_algorithm
        self.min_subarray_size = min_subarray_size
        self.max_subarray_size = max_subarray_size
        self.min_motif_count = min_motif_count
        self.min_cluster_size = min_cluster_size
        self.random_state = random_state
        random.seed(random_state)

        self.motives_db = defaultdict(list)
        self.motives = []
        self.clusters = defaultdict(list)
        self.cluster_models = []

    def save(self, model_name):
        self.clustering_algorithm = None  # NOTE: some algorithms cannot be pickled
        joblib.dump(self, model_name, compress=1)

    def load(self, obj):
        self.__dict__.update(joblib.load(obj).__dict__)
        self._build_cluster_models()

    def fit(self, seqs, neg_seqs=None):
        """Builds a discriminative estimator. 
        Identifies the maximal subarrays in the data. 
        Clusters them with the clustering algorithm provided in the initialization phase.
        For each cluster builds a fast sequence search model (Aho Corasick data structure). 
        """
        start = time()
        if self.training_size is None:
            training_seqs = seqs
        else:
            training_seqs = random.sample(seqs, self.training_size)
        self._fit_predictive_model(training_seqs, neg_seqs=neg_seqs)
        end = time()
        logger.info('model induction: %d positive instances %d secs' % (len(training_seqs), (end - start)))

        start = time()
        self.motives = self._motif_finder(seqs)
        end = time()
        logger.info('motives extraction: %d motives %d secs' % (len(self.motives), end - start))

        start = time()
        self._cluster(self.motives, clustering_algorithm=self.clustering_algorithm)
        end = time()
        logger.info('motives clustering: %d clusters %d secs' % (len(self.clusters), end - start))

        start = time()
        self._filter()
        end = time()
        n_motives = sum(len(self.motives_db[cid]) for cid in self.motives_db)
        n_clusters = len(self.motives_db)
        logger.info('after filtering: %d motives %d clusters %d secs' % (n_motives, n_clusters, (end - start)))

        start = time()
        # create models
        self._build_cluster_models()
        end = time()
        logger.info('motif model construction: %d secs' % (end - start))

    def fit_predict(self, seqs, return_list=False):
        self.fit(seqs)
        for prediction in self.predict(seqs, return_list=return_list):
            yield prediction

    def fit_transform(self, seqs, return_match=False):
        self.fit(seqs)
        for prediction in self.transform(seqs, return_match=return_match):
            yield prediction

    def predict(self, seqs, return_list=False):
        """Returns for each instance a list with the cluster ids that have a hit
        if  return_list=False then just return 1 if there is at least one hit from one cluster."""
        for header, seq in seqs:
            cluster_hits = []
            for cluster_id in self.motives_db:
                hits = self._cluster_hit(seq, cluster_id)
                if len(list(hits)):
                    cluster_hits.append(cluster_id)
            if return_list == False:
                if len(cluster_hits):
                    yield 1
                else:
                    yield 0
            else:
                yield cluster_hits

    def transform(self, seqs, return_match=False):
        """Transform an instance to a dense vector with features as cluster ID and entries 0/1 if a motif is found,
        if 'return_match' argument is True, then write a pair with (start position,end position)  in the entry instead of 0/1"""
        num = len(self.motives_db)
        for header, seq in seqs:
            cluster_hits = [0] * num
            for cluster_id in self.motives_db:
                hits = self._cluster_hit(seq, cluster_id)
                hits = list(hits)
                if return_match == False:
                    if len(hits):
                        cluster_hits[cluster_id] = 1
                else:
                    cluster_hits[cluster_id] = hits
            yield cluster_hits

    def _serial_graph_motif(self, seqs, placeholder=None):
        # make graphs
        iterable = sequence_to_eden(seqs)
        # use node importance and 'position' attribute to identify max_subarrays of a specific size
        graphs = self.vectorizer.annotate(iterable, estimator=self.estimator)
        # use compute_max_subarrays to return an iterator over motives
        motives = []
        for graph in graphs:
            subarrays = compute_max_subarrays(graph=graph, min_subarray_size=self.min_subarray_size, max_subarray_size=self.max_subarray_size)
            for subarray in subarrays:
                motives.append(subarray['subarray_string'])
        return motives

    def _multiprocess_graph_motif(self, seqs):
        size = len(seqs)
        intervals = compute_intervals(size=size, n_blocks=self.n_blocks, block_size=self.block_size)
        if self.n_jobs == -1:
            pool = mp.Pool()
        else:
            pool = mp.Pool(processes=self.n_jobs)
        results = [apply_async(pool, self._serial_graph_motif, args=(seqs[start:end], True)) for start, end in intervals]
        output = [p.get() for p in results]
        return list(chain(*output))

    def _motif_finder(self, seqs):
        if self.n_jobs > 1 or self.n_jobs == -1:
            return self._multiprocess_graph_motif(seqs)
        else:
            return self._serial_graph_motif(seqs)

    def _fit_predictive_model(self, seqs, neg_seqs=None):
        # duplicate iterator
        pos_seqs, pos_seqs_ = tee(seqs)
        pos_graphs = mp_pre_process(pos_seqs, pre_processor=sequence_to_eden,
                                    n_blocks=self.pre_processor_n_blocks,
                                    block_size=self.pre_processor_block_size,
                                    n_jobs=self.pre_processor_n_jobs)
        if neg_seqs is None:
            # shuffle seqs to obtain negatives
            neg_seqs = seq_to_seq(pos_seqs_, modifier=shuffle_modifier, times=self.negative_ratio, order=self.shuffle_order)
        neg_graphs = mp_pre_process(neg_seqs, pre_processor=sequence_to_eden,
                                    n_blocks=self.pre_processor_n_blocks,
                                    block_size=self.pre_processor_block_size,
                                    n_jobs=self.pre_processor_n_jobs)
        # fit discriminative estimator
        self.estimator = fit(pos_graphs, neg_graphs,
                             vectorizer=self.vectorizer,
                             n_iter_search=self.n_iter_search,
                             n_jobs=self.n_jobs,
                             n_blocks=self.n_blocks,
                             block_size=self.block_size,
                             random_state=self.random_state)

    def _cluster(self, seqs, clustering_algorithm=None):
        X = vectorize(seqs, vectorizer=self.seq_vectorizer, n_blocks=self.n_blocks, block_size=self.block_size, n_jobs=self.n_jobs)
        predictions = clustering_algorithm.fit_predict(X)
        # collect instance ids per cluster id
        for i in range(len(predictions)):
            self.clusters[predictions[i]] += [i]

    def _filter(self):
        # transform self.clusters that contains only the ids of the motives to
        # clustered_motives that contains the actual sequences
        new_sequential_cluster_id = -1
        clustered_motives = defaultdict(list)
        for cluster_id in self.clusters:
            if cluster_id != -1:
                if len(self.clusters[cluster_id]) >= self.min_cluster_size:
                    clustered_seqs = []
                    new_sequential_cluster_id += 1
                    for motif_id in self.clusters[cluster_id]:
                        clustered_motives[new_sequential_cluster_id].append(self.motives[motif_id])
        motives_db = defaultdict(list)
        # extract motif count within a cluster
        for cluster_id in clustered_motives:
            # consider only non identical motives
            motif_set = set(clustered_motives[cluster_id])
            for motif_i in motif_set:
                # count occurrences of each motif in cluster
                count = 0
                for motif_j in clustered_motives[cluster_id]:
                    if motif_i == motif_j:
                        count += 1
                # create dict with motives and their counts
                # if counts are above a threshold
                if count >= self.min_motif_count:
                    motives_db[cluster_id].append((count, motif_i))
        # transform cluster ids to incremental ids
        incremental_id = 0
        for cluster_id in motives_db:
            if len(motives_db[cluster_id]) >= self.min_cluster_size:
                self.motives_db[incremental_id] = motives_db[cluster_id]
                incremental_id += 1

    def _build_cluster_models(self):
        self.cluster_models = []
        for cluster_id in self.motives_db:
            motives = [motif for count, motif in self.motives_db[cluster_id]]
            cluster_model = esm.Index()
            for motif in motives:
                cluster_model.enter(motif)
            cluster_model.fix()
            self.cluster_models.append(cluster_model)

    def _cluster_hit(self, seq, cluster_id):
        for ((start, end), motif) in self.cluster_models[cluster_id].query(seq):
            yield (start, end)
