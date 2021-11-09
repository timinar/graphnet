import pandas as pd
import numpy as np
import sqlite3
import torch
from torch_geometric.data import Data
import time

class SQLiteDataset(torch.utils.data.Dataset):
    """Pytorch dataset for reading from SQLite.
    """
    def __init__(self, database, pulsemap_table, features, truth, index_column='event_no', truth_table='truth', selection=None, dtype=torch.float32):
    
        # Check(s)
        if isinstance(database, list):
            self._database_list = database
            self._all_connections_established = False
            self._all_connections = []
        else:
            self._database_list = None
            assert isinstance(database, str)
            assert database.endswith('.db')
        
        if isinstance(selection[0], list):
            self._indices = selection
        else:
            self._selection_list = None
            if selection != None:
                self._indices = selection
            else:
                self._indices = self._get_all_indices()

        assert isinstance(features, (list, tuple))
        assert isinstance(truth, (list, tuple))
        
        self._database = database
        self._pulsemap_table = pulsemap_table
        self._features = [index_column] + features
        self._truth = [index_column] + truth
        self._index_column = index_column
        self._truth_table = truth_table
        self._dtype = dtype

        self._features_string = ', '.join(self._features)
        self._truth_string = ', '.join(self._truth)
        if (self._database_list != None):
            self._current_database = None
        self._conn = None  # Handle for sqlite3.connection

        if selection is None:
            self._indices = self._get_all_indices()
        else:
            self._indices = selection
        self.close_connection()


    def __len__(self):
        return len(self._indices)

    def __getitem__(self, i):
        self.establish_connection(i)
        features, truth = self._query_database(i)
        graph = self._create_graph(features, truth)
        return graph

    def _get_all_indices(self):
        self.establish_connection(0)
        indices = pd.read_sql_query(f"SELECT {self._index_column} FROM {self._truth_table}", self._conn)
        return indices.values.ravel().tolist()

    def _query_database(self, i):
        """Query SQLite database for event feature and truth information.

        Args:
            i (int): Sequentially numbered index (i.e. in [0,len(self))) of the 
                event to query.

        Returns:
            list: List of tuples, containing event features.
            list: List of tuples, containing truth information.
        """
        if self._database_list == None:
            index = self._indices[i]
        else:
            index = self._indices[i][0]

        
        features = self._conn.execute(
            "SELECT {} FROM {} WHERE {} = {}".format(
                self._features_string, 
                self._pulsemap_table, 
                self._index_column, 
                index,
            )
        )

        truth = self._conn.execute(
            "SELECT {} FROM {} WHERE {} = {}".format(
                self._truth_string,
                self._truth_table,
                self._index_column,
                index,
            )
        )

        features = features.fetchall()
        truth = truth.fetchall()
        
        return features, truth

    def _create_graph(self, features, truth):
        """Create Pytorch Data (i.e.graph) object.

        No preprocessing is performed at this stage, just as no node adjancency 
        is imposed. This means that the `edge_attr` and `edge_weight` attributes 
        are not set.

        Args:
            features (list): List of tuples, containing event features.
            truth (list): List of tuples, containing truth information.

        Returns:
            torch.Data: Graph object.
        """
        # Convert nested list to simple dict
        truth_dict = {key: truth[0][ix] for ix, key in enumerate(self._truth)}
        assert len(truth) == 1

        # Unpack common variables
        abs_pid = abs(truth_dict['pid'])
        sim_type = truth_dict['sim_type']
        
        labels_dict = {
            'event_no': truth_dict['event_no'],
            'muon': abs_pid == 13,
            'muon_stopped': truth_dict.get('stopped_muon') == 1,
            'noise': (abs_pid == 1) & (sim_type != 'data'),
            'neutrino': (abs_pid != 13 ) & (abs_pid != 1 ),  # `abs_pid in [12,14,16]`?
            'v_e': abs_pid == 12,
            'v_u': abs_pid == 14,
            'v_t': abs_pid == 16,
            'track': (abs_pid == 14) & (truth_dict['interaction_type'] == 1),
            'dbang': sim_type == 'dbang',
            'corsika': abs_pid > 20,
        }

        x = torch.tensor(np.asarray(features)[:,1:], dtype=self._dtype) 
        n_pulses = torch.tensor(len(x), dtype=torch.int32)
        graph = Data(
            x=x,
            edge_index= None
        )
        graph.n_pulses = n_pulses

        for key, value in labels_dict.items():
            try:
                graph[key] = torch.tensor(value)
            except TypeError:
                # Cannot convert `value` to Tensor due to its data type, e.g. `str`.
                pass

        for key, value in truth_dict.items():
            try:
                graph[key] = torch.tensor(value)
            except TypeError:
                # Cannot convert `value` to Tensor due to its data type, e.g. `str`.
                pass


        return graph
        
    def establish_connection(self,i):
        """Make sure that a sqlite3 connection is open."""
        if self._database_list == None:
            if self._conn is None:
                self._conn = sqlite3.connect(self._database)
        else:
            if self._conn is None:
                if self._all_connections_established is False:
                    self._all_connections = []
                    for database in self._database_list:
                        con = sqlite3.connect(database)
                        self._all_connections.append(con)
                    self._all_connections_established = True
                self._conn = self._all_connections[self._indices[i][1]]
            if self._indices[i][1] != self._current_database:
                self._conn = self._all_connections[self._indices[i][1]]
                self._current_database = self._indices[i][1]
        return self

    def close_connection(self):
        """Make sure that no sqlite3 connection is open.

        This is necessary to calls this before passing to `torch.DataLoader` 
        such that the dataset replica on each worker is required to create its 
        own connection (thereby avoiding `sqlite3.DatabaseError: database disk 
        image is malformed` errors due to inability to use sqlite3 connection 
        accross processes.
        """
        if self._conn is not None:
            self._conn.close()
            del self._conn
            self._conn = None
        if self._database_list != None:
            if self._all_connections_established == True:
                for con in self._all_connections:
                    con.close()
                del self._all_connections
                self._all_connections_established = False
                self._conn = None
        return self