import numpy as np
import pandas as pd
import os
import sys
from abstract_lungair_data import AbstractLungairData

dtype_string_mapping = { # Map schema dtype string to pandas dtype
  'int4' : "int32_possibly_nan",
  # Why is int4 being treated specially? For easier NaN support; see
  # https://pandas.pydata.org/pandas-docs/stable/user_guide/gotchas.html#support-for-integer-na
  # It just happened to be in these tables that int4 columns were the ones that sometimes had NaNs
  'int2' : np.int16,
  'varchar' : str,
  'numeric' : np.float32,
  'timestamp' : np.datetime64, # pandas handles this differently-- we will deal with the exception below
  'float8' : np.double
}


def get_dtype_dict(pasted_table_path):
  """Given the path to a schema description text file, read out a dictionary that describes the schema."""
  dtype_dict = {}
  with open(pasted_table_path) as f:
    for line in f.readlines():
      try:
        column_name, dtype_string = line.split()[:2]
      except BaseException as e:
        print(f"The schema description at {pasted_table_path} might not be formatted correctly.", file=sys.stderr)
        raise
      if dtype_string not in dtype_string_mapping.keys():
        raise KeyError(f"Please add an entry for {dtype_string} to dtype_string_mapping")
      mapped_string = dtype_string_mapping[dtype_string]
      dtype_dict[column_name.upper()] = mapped_string
  return dtype_dict


class Mimic3(AbstractLungairData):
  """This class handles loading the tables we want from the MIMIC-III dataset"""

  def initData(self, mimic3_data_dir, mimic3_schemas_dir):
    """
    Given the path to the mimic3 dataset and the path to the schema text files,
    load into memory the tables that we care about.
    """

    if not os.path.isdir(mimic3_data_dir):
      raise FileNotFoundError(f"Please provide a valid directory for the MIMIC-III data directory; received: {mimic3_data_dir}")
    if not os.path.isdir(mimic3_schemas_dir):
      raise FileNotFoundError(f"Please provide a valid directory for the MIMIC-III schema descriptions; received: {mimic3_schemas_dir}")

    self.data_dir = mimic3_data_dir
    self.schemas_dir = './mimic3-schemas/'
    self.ICUSTAYS = self.read_table('ICUSTAYS', 'ICUSTAY_ID')
    self.NICUSTAYS = self.ICUSTAYS[self.ICUSTAYS.FIRST_CAREUNIT == 'NICU']
    self.PATIENTS = self.read_table('PATIENTS', 'SUBJECT_ID')
    self.NICU_PATIENTS = self.PATIENTS.loc[self.NICUSTAYS.SUBJECT_ID.astype(int)]
    self.D_ITEMS = self.read_table('D_ITEMS', "ITEMID")

    self.nicu_stay_ids = set(self.NICUSTAYS.index)

    with self.read_table("CHARTEVENTS", index_col="ROW_ID", chunksize=1e6) as reader:
      for n, chunk in enumerate(reader):
        nicu_chartevents_chunk = chunk[chunk.ICUSTAY_ID.isin(self.nicu_stay_ids)]
        if n==0:
          self.NICU_CHARTEVENTS = nicu_chartevents_chunk
        else:
          self.NICU_CHARTEVENTS = pd.concat([self.NICU_CHARTEVENTS,nicu_chartevents_chunk])
        print(f'chunk {n}/330 done, collected {len(self.NICU_CHARTEVENTS)} samples in total')

    # Select only the chart events that we support converting into Observation resource
    self.NICU_CHARTEVENTS_SUPPORTED = self.NICU_CHARTEVENTS[
      self.NICU_CHARTEVENTS.ITEMID.isin(self.ITEM_IDS.values()) # ensure that item id is a supported one
      & (self.NICU_CHARTEVENTS.STOPPED=='NotStopd') # ensure that the measurement was not discontinued
      & (~self.NICU_CHARTEVENTS.VALUENUM.isna()) # ensure that numerical value is not nan
    ]


  def read_table(self, table_name, index_col=None, chunksize=None):
    """
    Load a DataFrame using pandas read_csv.

    Args:
      table_name: The name of the csv file. The corresponding schema description text file is expected to have the same basename.
      index_col: Name of the column to be used as an index for the DataFrame
      chunksize: If set to not none, then this is the number of rows to read at a time.
        When this options is used, a context manager TextFileReader is returned, rather than a DataFrame
    """

    schema_description_path = os.path.join(self.schemas_dir,f'{table_name}.txt')
    if not os.path.isfile(schema_description_path):
      raise FileNotFoundError(f"Could not find schema description text file for the {table_name} table at {schema_description_path}.")

    dtype_dict = get_dtype_dict(schema_description_path)
    parse_int = [index_col] # if index col is int, definitely parse it that way b/c it should be NaN anyway

    # It makes sense to also parse all the ID columns as int,
    # however some ID columns in CHARTEVENTS cause trouble, so commenting this out:
    # parse_int += [colname for colname in dtype_dict if '_ID' in colname]

    date_cols = []
    for colname in list(dtype_dict):
      if dtype_dict[colname] == np.datetime64:
        dtype_dict.pop(colname)
        date_cols.append(colname)
        continue
      # We use float in the following for better NaN support, except what we added to parse_int
      if dtype_dict[colname] == "int32_possibly_nan":
        dtype_dict[colname] = np.int32 if colname in parse_int else float

    table_path = os.path.join(self.data_dir,f'{table_name}.csv.gz')
    # if table_name == 'CHARTEVENTS':
    #   table_path = './CHARTEVENTS_test.csv'
    # print(dtype_dict)

    return pd.read_csv(
      table_path,
      index_col = index_col,
      dtype=dtype_dict,
      chunksize=chunksize,
      parse_dates = date_cols
    )

  def get_all_patients(self):
    return (data for _,data in self.NICU_PATIENTS.iterrows())

  def get_patient_chart_events(self, patientName):
    patient_chart_events =\
      self.NICU_CHARTEVENTS_SUPPORTED[self.NICU_CHARTEVENTS_SUPPORTED.SUBJECT_ID == patientName]

    return (data for _,data in patient_chart_events.iterrows())

  def get_patient_gender(self, patient_info):
    return patient_info.GENDER

  def get_patient_system(self):
    return 'https://mimic.mit.edu/docs/iii/tables/patients/#subject_id'

  def get_patient_id(self, patient_info):
    return patient_info.name

  def get_patient_dob(self, patient_info):
    return patient_info.DOB

  def get_observation_item_id(self, observation_info):
    return observation_info.ITEMID

  def get_observation_row_id(self, observation_info):
    return observation_info.name

  def get_observation_fio2(self, observation_info):
    return observation_info.VALUENUM

  def get_observation_pip(self, observation_info):
    return observation_info.VALUENUM

  def get_observation_peep(self, observation_info):
    return observation_info.VALUENUM

  def get_observation_hr(self, observation_info):
    return observation_info.VALUENUM

  def get_observation_sao2(self, observation_info):
    return observation_info.VALUENUM

  def get_observation_unit_string(self, observation_info):
    return observation_info.VALUEUOM

  def get_observation_system(self):
    return 'ROW_ID in https://mimic.mit.edu/docs/iii/tables/chartevents/'

  def get_observation_time(self, observation_info):
    return observation_info.CHARTTIME