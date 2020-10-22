import copy
import logging
from pathlib import Path

import pandas as pd

from .container_operations import export_session, find_or_create_group

log = logging.getLogger(__name__)

OHIF_CONFIG = "/flywheel/v0/ohif_config.json"


class InvalidGroupError(Exception):
    """
    Exception raised for using an Invalid Flywheel group for this gear.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


class InvalidInputError(Exception):
    """
    Exception raised for using an Invalid Input for this gear.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


class NoReaderProjectsError(Exception):
    """
    Exception raised when no reader projects exist to populate.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


class InvalidLaunchContainerError(Exception):
    """
    Exception raised when gear is launched from an invalid container.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


class InvalidReaderError(Exception):
    """
    Exception raised for referencing an invalid reader of a project.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


class ExistingReaderCaseError(Exception):
    """
    Exception raised for attempted re-export of a case.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


class ExceededConstraintsError(Exception):
    """
    Exception raised when constraints are exceeded.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


class MissingDataError(Exception):
    """
    Exception raised when required data is missing.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


def set_session_features(session, case_coverage):
    """
    Gets or sets session features and updates later

    Each session has a set of features: case_coverage, assignments, and assignment_count
    each assignment consists of {project_id:<uid>, session_id:<uid>, status:<str>}
    Once diagnosed and measured each assignment will have the additional tags of
    {measurements:{}, read: {}} that are produced as part of the measurement process.
    if not found, create with defaults

    Args:
        session (flywheel.Session): The session to set/retrieve features from
    """

    session_features = (
        session.info["session_features"]
        if session.info.get("session_features")
        else {"case_coverage": case_coverage, "assignments": [], "assigned_count": 0}
    )

    return session_features


def set_project_session_attributes(session_features):
    """
    Return session attributes generated by assigning sessions to reader projects

    Args:
        session_features (dict): The session features (how many assignments) of the
            above session

    Returns:
        dict: The compiled attributes of a session for recording at the project level
    """
    session_attributes = {
        "id": session_features["id"],
        "label": session_features["label"],
        "case_coverage": session_features["case_coverage"],
        "unassigned": session_features["case_coverage"]
        - len(session_features["assignments"]),
        "assigned": len(session_features["assignments"]),
        "diagnosed": len(
            [
                assignment["status"]
                for assignment in session_features["assignments"]
                if assignment["status"] == "Diagnosed"
            ]
        ),
        "measured": len(
            [
                assignment["status"]
                for assignment in session_features["assignments"]
                if assignment["status"] == "Measured"
            ]
        ),
        "completed": len(
            [
                assignment["status"]
                for assignment in session_features["assignments"]
                if assignment["status"] == "Completed"
            ]
        ),
    }

    return session_attributes


def check_valid_reader(fw_client, reader_id, group_id):
    """
    Checks for the existing reader project for indicated reader_id.

    Args:
        fw_client (flywheel.Client): Flywheel instance client for api calls.
        reader_id (str): The email of the reader to validate
        group_id (str): The id of the reader group
        reader_project_id (str): Passes the reader's project id back if found.
    Raises:
        InvalidReaderError: If a project is not found assigned to reader, raised to exit

    Returns:
        str: Returns reader's project id on success, `None` on failure
    """

    group_projects = fw_client.projects.find(f'group="{group_id}"')

    proj_roles = [
        role.id
        for role in fw_client.get_all_roles()
        if role.label in ["read-write", "read-only"]
    ]

    valid_reader_ids = [
        [
            perm.id
            for perm in proj.permissions
            if set(perm.role_ids).intersection(proj_roles)
        ][0]
        for proj in group_projects
    ]

    reader_project = None

    if reader_id in valid_reader_ids:
        reader_project = [
            proj
            for proj in group_projects
            if reader_id
            in [
                perm.id
                for perm in proj.permissions
                if set(perm.role_ids).intersection(proj_roles)
            ]
        ][0]

    return reader_project


def initialize_dataframes(fw_client, reader_group):
    """
    Initializes pandas DataFrames used to select sessions and reader projects

    Args:
        fw_client (flywheel.Client): Flywheel Client object instantiated on instance
        reader_group (flywheel.Group): The reader group

    Returns:
        tuple: a pair of pandas DataFrames representing the source sessions and
            destination projects
    """

    # This dataframe is to keep track of the sessions each reader project has and the
    # total number of those sessions. Initialized below.
    dest_projects_df = pd.DataFrame(
        columns=[
            "id",
            "label",
            "reader_id",
            "assignments",
            "max_cases",
            "num_assignments",
        ],
        dtype="object",
    )

    # Initialize destination projects dataframe
    for reader_proj in fw_client.projects.find(f'group="{reader_group.id}"'):
        reader_proj = reader_proj.reload()
        project_features = reader_proj.info["project_features"]
        # Valid roles for readers are "read-write" and "read-only"
        proj_roles = [
            role.id
            for role in fw_client.get_all_roles()
            if role.label in ["read-write", "read-only"]
        ]
        reader_id = [
            perm.id
            for perm in reader_proj.permissions
            if set(perm.role_ids).intersection(proj_roles)
        ][0]
        # Fill the dataframe with project data.
        dest_projects_df.loc[dest_projects_df.shape[0] + 1] = [
            reader_proj.id,
            reader_proj.label,
            reader_id,
            project_features["assignments"],
            project_features["max_cases"],
            len(reader_proj.sessions()),
        ]

    # This dataframe keeps track of each reader project and session each session was
    # exported to.
    source_sessions_df = pd.DataFrame(
        columns=["id", "label", "assignments", "assigned_count"]
    )

    return source_sessions_df, dest_projects_df


def confirm_or_create_ohif_config(master_project):
    """
    Confirms or creates ohif_config.json in master project.

    The ohif_config.json file determines the functionality and presentation of the
    ohifViewer for this project.

    TODO: Some mechanism to verify that the master project has the most recent
    ohif_config.json.

    Args:
        master_project (flywheel.Project): The Master Project with the ohif_config.json.
    """
    ohif_config_path = "/tmp/ohif_config.json"
    if master_project.get_file("ohif_config.json"):
        master_project.download_file("ohif_config.json", ohif_config_path)
        # TODO: This is where we would compare them.
    else:
        master_project.upload_file(OHIF_CONFIG)


def check_valid_case_assignment(
    fw_client, session_id, reader_email, reader_group_id, reader_row, case_coverage
):
    """
    Checks the validity of a case/reader assignment.

    Args:
        fw_client (flywheel.Client): Active Flywheel client object
        session_id (str): The id of the session to assign to a `reader_email`.
        reader_email (str): The email of the reader to assign the `session_id` to.
        reader_group_id (str): The `id` of the reader group to check for projects.
        reader_row (pandas.Series): The dataframe row depicting a reader's assignments.
        case_coverage (int): The maximum number of assignments for a session/case.

    Returns:
        tuple: (valid, message) indicating if valid and a message if not.
    """

    # Check for valid session
    src_session = fw_client.sessions.find_first(f"_id={session_id}")
    if not src_session:
        message = (
            f"Session with id ({session_id}) is not found within a Master Project. "
            f"Proceeding without making this assignment to reader ({reader_email})."
        )
        return False, message
    else:
        src_session = src_session.reload()

    # Check for the forbidden group
    # TODO: Derive a test...tricky... because of created projects and sessions.
    if src_session.parents["group"] is reader_group_id:
        message = (
            f"Session with id ({session_id}) belongs to a reader project.\n"
            f"Please correctly identify the session in a Master Project and try again."
        )
        return False, message

    # Check for valid Reader
    reader_proj = check_valid_reader(fw_client, reader_email, reader_group_id)
    if not reader_proj:
        message = (
            f"The reader, {reader_email}, has not been established. "
            "Please run `assign-readers` to establish a project for this reader"
        )
        return False, message

    # Check for the existence of the selected session in the reader project
    if src_session.label in [sess.label for sess in reader_proj.sessions()]:
        message = (
            f"Selected session ({src_session.label}) has already been assigned to "
            f"reader ({reader_email})."
        )
        return False, message

    # Check reader availability
    if reader_row.num_assignments == reader_row.max_cases:
        message = (
            f"Cannot assign more than {reader_row.max_cases} cases to "
            f"reader ({reader_email}). "
            "Consider increasing max_cases for this reader "
            "or choosing another reader."
        )
        return False, message

    # Check session to ensure num_assignments < case_coverage
    session_features = set_session_features(src_session, case_coverage)
    if session_features["assigned_count"] == session_features["case_coverage"]:
        message = (
            f"Assigning this case ({src_session.label}) exceeds "
            f"case_coverage ({session_features['case_coverage']}) for this case."
            "Assignment will not proceed."
        )
        return False, message

    return True, "All validation checks, passed."


def distribute_batch_to_readers(
    fw_client, source_project, reader_group_id, case_coverage, batch_csv_path
):
    """
    Distribute batch of cases (sessions) from a source project to reader projects.

    Each case (session) is exported to readers indicated in required csv until the
    max_cases of each reader is achieved or the case_coverage of each session has been
    met. If an assignment would break the max_cases or the case_coverage constraints,
    the assignment is not made and a warning is logged without failure.

    Args:
        fw_client (flywheel.Client): An instantiated Flywheel Client to host instance
        source_project (flywheel.Project): The source project for all sessions
        reader_group_id (str): The Flywheel container id for the group in question
        case_coverage (int): The default number of readers assigned to each session
        batch_csv_path (str): Path to batch csv with case-reader assignments.
    Returns:
        tuple: Pandas DataFrames recording source and destination for
            each session exported.
    """

    # Grab project-level features, if it does not exist, set defaults
    project_features = (
        source_project.info["project_features"]
        if source_project.get("project_features")
        else {"case_coverage": case_coverage, "case_states": []}
    )
    project_info = {}
    # Ensure a valid ohif_config.json file is present for the master project
    confirm_or_create_ohif_config(source_project)

    # Keep track of all the exported and created data
    # On Failure, remove contents of created_data from instance.
    exported_data = []
    created_data = []

    # Find or create reader group
    reader_group, _created_data = find_or_create_group(
        fw_client, reader_group_id, "Readers"
    )
    created_data.extend(_created_data)

    # Initialize dataframes used to select sessions and readers without replacement
    source_sessions_df, dest_projects_df = initialize_dataframes(
        fw_client, reader_group
    )

    # If the dataframe for destination projects is empty, raise an error.
    if dest_projects_df.shape[0] == 0:
        raise NoReaderProjectsError(
            "Readers have not been added to this project. "
            "Please run `assign-readers` with valid configuration first."
        )

    # Load and Check batch dataframe
    batch_df = pd.read_csv(batch_csv_path)

    # check for required columns:
    req_columns = ["session_id", "session_label", "reader_email"]
    if not all([(c in batch_df.columns) for c in req_columns]):
        joined_cols = ", ".join(req_columns)
        raise InvalidInputError(
            f"The csv-file ({Path(batch_csv_path).name}) did not have the required "
            + f"columns ({joined_cols}). "
            + "Cannot continue."
        )

    batch_df["passed"] = True
    batch_df["message"] = ""

    # Loop through dataframe, check session_id, reader_email
    for i in batch_df.index:
        # Check for valid session
        session_id = batch_df.session_id[i]
        reader_email = batch_df.reader_email[i]
        src_session = fw_client.sessions.find_first(f"_id={session_id}")

        if src_session:
            src_session = src_session.reload()
            session_features = set_session_features(src_session, case_coverage)
        else:
            session_features = {}

        # Locate Reader Project
        if reader_email in dest_projects_df.reader_id.values:
            indx = dest_projects_df[dest_projects_df.reader_id == reader_email].index[0]
            project_id = dest_projects_df.id[indx]
            reader_proj = fw_client.get(project_id).reload()
        # This will be caught as a non-valid reader,
        # padding these variables to pass then to validation function.
        # TODO: find a more graceful way to do this
        else:
            indx = dest_projects_df.index[0]
            project_id = dest_projects_df.id[indx]
            reader_proj = None

        reader_row = dest_projects_df.loc[indx, :]

        valid, message = check_valid_case_assignment(
            fw_client,
            session_id,
            reader_email,
            reader_group_id,
            reader_row,
            case_coverage,
        )

        if not valid:
            batch_df.loc[i, "passed"] = False
            batch_df.loc[i, "message"] = message
            log.error(message)
            continue

        # With checks complete, assign indicated case to selected reader
        try:
            # export the session to the reader project
            dest_session, _exported_data, _created_data = export_session(
                fw_client, src_session, fw_client.get(project_id)
            )

            exported_data.extend(_exported_data)
            created_data.extend(_created_data)

        except Exception as e:
            log.warning("Error while exporting a session, %s.", src_session.label)
            log.exception(e)
            log.warning("Examine the data and try again.")
            continue

        # record source and dest session ids in destination project dataframe
        if not dest_projects_df.loc[indx, "assignments"]:
            dest_projects_df.loc[indx, "assignments"] = [
                {"source_session": src_session.id, "dest_session": dest_session.id}
            ]
        else:
            dest_projects_df.loc[indx, "assignments"].append(
                {"source_session": src_session.id, "dest_session": dest_session.id}
            )

        dest_projects_df.loc[indx, "num_assignments"] += 1
        session_features["assigned_count"] += 1
        session_features["assignments"].append(
            {
                "project_id": project_id,
                "reader_id": reader_email,
                "session_id": dest_session.id,
                "status": "Assigned",
            }
        )

        # Record updates to the source session
        session_info = {"session_features": session_features}
        src_session.update_info(session_info)

        # update reader project from updates to the dataframe
        project_info = {
            "project_features": {
                "assignments": dest_projects_df.loc[indx, "assignments"],
                "max_cases": dest_projects_df.loc[indx, "max_cases"],
            }
        }
    if project_info:
        reader_proj.update_info(project_info)

    # Iterate through sessions to record system state of Assigned Sessions
    for tmp_session in source_project.sessions():
        tmp_session = tmp_session.reload()
        session_features = set_session_features(tmp_session, 3)
        # always record the state in the dataframe.
        session_features["id"] = tmp_session.id
        session_features["label"] = tmp_session.label
        source_sessions_df = source_sessions_df.append(
            session_features, ignore_index=True
        )

        project_session_attributes = set_project_session_attributes(session_features)

        # Check to see if the case is already present in the project_features
        case = [
            case
            for case in project_features["case_states"]
            if case and (case["id"] == session_features["id"])
        ]
        if case:
            index = project_features["case_states"].index(case[0])
            project_features["case_states"].pop(index)

        # append new or updated case data to project_features
        project_features["case_states"].append(project_session_attributes)

    source_project.update_info({"project_features": project_features})
    # Create a DataFrame from exported_data and then export
    exported_data_df = pd.DataFrame(data=exported_data)

    return source_sessions_df, dest_projects_df, exported_data_df, batch_df
